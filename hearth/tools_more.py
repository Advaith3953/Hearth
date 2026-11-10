"""More abilities: compare files, find and move duplicates, tidy a folder, bulk rename, merge/extract PDFs, images to PDF, fill a
Word template, make a spreadsheet, look inside zip files, show and undo history, and find dates in documents.

Same rules as every other tool: listing is free, reading content asks, every change asks first, nothing is overwritten, and a
change made to many files at once is one entry in the undo journal, so one undo puts all of it back."""
from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import docxio, readers, sheets
from .access import AccessError
from .filetypes import LABELS, ext, kind_of, normalize_kind
from .tools import _P, TOOLS, _finish_write, _size, _when
from .tools_base import Budget, Ctx, Tool, ToolError, default_scope, need_read, read_path, walk

_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


# ---------------------------------------------------------------- shared helpers

def _list(raw: str) -> list[str]:
    """'a.pdf; b.pdf', 'a.pdf, b.pdf' or one name per line -> names."""
    parts = [p for p in re.split(r"[\n;|]+", raw) if p.strip()]
    if len(parts) == 1 and "," in parts[0]:
        parts = parts[0].split(",")
    return [p.strip().strip("\"'") for p in parts if p.strip().strip("\"'")]


def _src(ctx: Ctx, raw: str, prefer: tuple[str, ...] | None = None, near: str | None = None) -> str:
    """Resolve a file the user named. A bare name that is not found in the usual places is also looked for in the folder
    named as the destination ("merge a and b in E:\\Reports")."""
    try:
        return read_path(ctx, raw, prefer=prefer)
    except AccessError:
        if near and not os.path.isabs(raw) and not re.match(r"^[A-Za-z]:", raw):
            folder = str(ctx.access.resolve(near, must_exist=False))
            if os.path.isdir(folder):
                return read_path(ctx, os.path.join(folder, raw), prefer=prefer)
        raise


def _clean_name(name: str) -> str:
    name = name.strip()
    if not name or _BAD_NAME.search(name) or name in (".", ".."):
        raise ToolError(f"'{name}' cannot be used as a name (no \\ / : * ? \" < > |)")
    return name


def _new_file(ctx: Ctx, raw_dst: str | None, name: str | None, default_dir: str, suffix: str, default_name: str) -> str:
    """Where a NEW file goes: the folder or drive named (else default_dir), called `name` (else default_name), with `suffix`.
    Never an existing file, never outside what may be changed."""
    target = str(ctx.access.resolve(raw_dst, must_exist=False)) if raw_dst and raw_dst.strip() else default_dir
    ctx.access.check_writable(target)
    if os.path.isdir(target):
        base = name.strip() if name and name.strip() else default_name
        folder = target
    else:                                       # the destination itself is the file name
        folder, base = os.path.dirname(target), (name.strip() if name and name.strip() else os.path.basename(target))
    if suffix and base.lower().endswith(suffix):
        base = base[: -len(suffix)]
    dst = os.path.join(folder, _clean_name(base) + suffix)
    ctx.access.check_writable(dst)
    if os.path.lexists(dst):
        raise ToolError(f"{dst} already exists; Hearth never overwrites. Choose another name")
    return dst


def _folder(ctx: Ctx, raw: str | None) -> str:
    if raw and raw.strip():
        p = str(ctx.access.resolve(raw))
    else:
        roots = default_scope(ctx)
        p = roots[0]
    if not os.path.isdir(p):
        raise ToolError(f"{p} is a file, not a folder")
    return p


def _lines(items: list[str], limit: int = 12) -> str:
    shown = items[:limit]
    return "\n".join(shown) + (f"\n... and {len(items) - limit} more" if len(items) > limit else "")


def _batch(ctx: Ctx, items: list[dict[str, Any]]) -> None:
    """One journal entry for the whole request, so a single undo reverses all of it."""
    if len(items) > 1:
        ctx.journal.record("batch", items=items)
    else:
        op = items[0]["op"]
        ctx.journal.record(op, **{k: v for k, v in items[0].items() if k != "op"})
    ctx.index.sync()


# ---------------------------------------------------------------- 1. compare two files

def _grid(path: str) -> dict[str, dict[tuple[int, int], str]]:
    return {sh.name: {(r, c): v for r, row in sh.cells.items() for c, v in row.items() if v != ""} for sh in sheets.load(path)}


def _digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def compare_files(ctx: Ctx, a: dict[str, Any]) -> str:
    p1, p2 = read_path(ctx, a["first"]), read_path(ctx, a["second"])
    if os.path.isdir(p1) or os.path.isdir(p2):
        raise ToolError("compare_files compares two files, not folders")
    n1, n2 = os.path.basename(p1), os.path.basename(p2)
    head = f"Comparing {p1}  with  {p2}"
    try:
        if kind_of(p1) == "excel" and kind_of(p2) == "excel":
            g1, g2 = _grid(p1), _grid(p2)
            out: list[str] = []
            for name in sorted(set(g1) | set(g2)):
                if name not in g1 or name not in g2:
                    out.append(f"sheet '{name}' only exists in {n1 if name in g1 else n2}")
                    continue
                for pos in sorted(set(g1[name]) | set(g2[name])):
                    x, y = g1[name].get(pos, ""), g2[name].get(pos, "")
                    if x != y:
                        out.append(f"{name}!{sheets.col_letters(pos[1])}{pos[0]}: '{x}' -> '{y}'")
            if not out:
                return f"{head}\nThe two spreadsheets hold the same values in every cell."
            return f"{head}\n{len(out)} cell difference(s) (first file -> second file):\n" + _lines(out, 40)
        text = [readers.read_text(p) for p in (p1, p2)]
    except (readers.UnreadableError, sheets.SheetError):
        same = os.path.getsize(p1) == os.path.getsize(p2) and _digest(p1) == _digest(p2)
        return head + ("\nThe two files are identical, byte for byte." if same else
                       f"\nThe files differ ({_size(os.path.getsize(p1))} vs {_size(os.path.getsize(p2))}); "
                       "they are not text, so the difference cannot be shown.")
    if text[0] == text[1]:
        return f"{head}\nThe text of the two files is identical."
    l1, l2 = text[0].splitlines(), text[1].splitlines()
    diff = [d for d in difflib.unified_diff(l1, l2, n1, n2, n=0, lineterm="") if not d.startswith(("---", "+++", "@@"))]
    added, removed = sum(d.startswith("+") for d in diff), sum(d.startswith("-") for d in diff)
    shown = [d[:160] for d in diff[:40]]
    return (f"{head}\n{removed} line(s) only in {n1} (-), {added} line(s) only in {n2} (+):\n" + "\n".join(shown)
            + (f"\n... {len(diff) - 40} more" if len(diff) > 40 else ""))


# ---------------------------------------------------------------- 2. duplicates

def _dup_groups(ctx: Ctx, root: str, kind: str, min_bytes: int) -> tuple[list[list[str]], bool]:
    """Groups of files with exactly the same content (size first, then a hash), biggest waste first."""
    budget = Budget(seconds=25.0, files=200_000)
    by_size: dict[int, list[str]] = defaultdict(list)
    for dp, _, files in walk(ctx, root, budget):
        for f in files:
            p = os.path.join(dp, f)
            if kind and kind_of(p) != kind:
                continue
            try:
                if os.path.islink(p):
                    continue
                size = os.path.getsize(p)
            except OSError:
                continue
            if size >= min_bytes:
                by_size[size].append(p)
    groups: list[list[str]] = []
    for paths in by_size.values():
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[str]] = defaultdict(list)
        for p in paths:
            try:
                by_hash[_digest(p)].append(p)
            except OSError:
                continue
        groups += [sorted(g, key=lambda x: (os.path.getmtime(x), len(x), x)) for g in by_hash.values() if len(g) > 1]
    groups.sort(key=lambda g: -os.path.getsize(g[0]) * (len(g) - 1))
    return groups, budget.truncated


def _dup_args(ctx: Ctx, a: dict[str, Any]) -> tuple[str, str, int]:
    root = _folder(ctx, a.get("folder"))
    kind = normalize_kind(str(a.get("kind", "")))
    if kind is None:
        raise ToolError("kind must be excel, word, pdf, image, ... (or leave it out)")
    need_read(ctx, root)                    # comparing files means reading them
    return root, kind, max(1, int(a.get("min_kb", 0)) * 1024)


def find_duplicates(ctx: Ctx, a: dict[str, Any]) -> str:
    root, kind, min_bytes = _dup_args(ctx, a)
    groups, cut = _dup_groups(ctx, root, kind, min_bytes)
    if not groups:
        return f"No duplicate files under {root}." + (" (The scan stopped at its limit.)" if cut else "")
    waste = sum(os.path.getsize(g[0]) * (len(g) - 1) for g in groups)
    ctx.found = [p for g in groups for p in g][:200]
    out = [f"Group {i} - {len(g)} identical copies of {_size(os.path.getsize(g[0]))}:\n" + "\n".join(
        f"    {p}  (modified {_when(os.path.getmtime(p))})" for p in g) for i, g in enumerate(groups[:15], start=1)]
    return (f"{len(groups)} group(s) of identical files under {root}; {_size(waste)} could be freed.\n" + "\n".join(out)
            + (f"\n... {len(groups) - 15} more group(s)" if len(groups) > 15 else "")
            + ("\n(The scan stopped at its limit.)" if cut else "")
            + "\nTo clear them: move_duplicates keeps the oldest copy of each and moves the rest into a _duplicates folder.")


def move_duplicates(ctx: Ctx, a: dict[str, Any]) -> str:
    root, kind, min_bytes = _dup_args(ctx, a)
    ctx.access.check_writable(root)
    groups, cut = _dup_groups(ctx, root, kind, min_bytes)
    extras = [p for g in groups for p in g[1:]
              if "_duplicates" not in Path(p).parts and not ctx.access.is_blocked(p) and not ctx.access.is_system(p)]
    if not extras:
        return f"No duplicate files to move under {root}."
    bin_dir = os.path.join(root, "_duplicates")
    total = sum(os.path.getsize(p) for p in extras)
    detail = (f"{len(extras)} extra copies ({_size(total)}) go into {bin_dir}. The oldest copy of each stays where it is. "
              "Nothing is deleted, and undo puts every file back.\n" + _lines([os.path.relpath(p, root) for p in extras]))
    if not ctx.ask(f"Move {len(extras)} duplicate file(s) into _duplicates?", detail, risky=False, where=bin_dir):
        return "DENIED: the user said no. Nothing was changed."
    items: list[dict[str, Any]] = []
    if not os.path.isdir(bin_dir):
        os.makedirs(bin_dir)
        items.append({"op": "create", "path": bin_dir})
    for p in extras:
        dst = os.path.join(bin_dir, os.path.basename(p))
        n = 2
        while os.path.lexists(dst):
            stem, e = os.path.splitext(os.path.basename(p))
            dst = os.path.join(bin_dir, f"{stem} ({n}){e}")
            n += 1
        shutil.move(p, dst)
        items.append({"op": "move", "src": p, "dst": dst})
    _batch(ctx, items)
    return f"OK: moved {len(extras)} duplicate file(s) ({_size(total)}) from {root} into {bin_dir}; the oldest copy of each stayed" \
           + (" (the scan stopped at its limit, so ask again for more)" if cut else "")


# ---------------------------------------------------------------- 3. tidy a folder

_DATE_FMT = "%Y-%m"


def tidy_folder(ctx: Ctx, a: dict[str, Any]) -> str:
    root = _folder(ctx, a.get("folder"))
    ctx.access.check_writable(root)
    by = str(a.get("by", "type")).strip().lower() or "type"
    if by not in ("type", "date", "extension"):
        raise ToolError("by must be type, date or extension")
    plan: dict[str, list[str]] = defaultdict(list)
    skipped: list[str] = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if name.startswith(".") or not os.path.isfile(p) or os.path.islink(p) or ctx.access.is_blocked(p):
            continue
        if by == "type":
            group = LABELS[kind_of(p)]
        elif by == "date":
            group = time.strftime(_DATE_FMT, time.localtime(os.path.getmtime(p)))
        else:
            group = (ext(p).lstrip(".").upper() or "No extension")
        if os.path.lexists(os.path.join(root, group, name)):
            skipped.append(name)
            continue
        plan[group].append(name)
        if sum(map(len, plan.values())) >= 500:
            break
    count = sum(map(len, plan.values()))
    if not count:
        return f"Nothing to tidy: {root} has no loose files to sort." + (f" ({len(skipped)} already exist in their folder.)" if skipped else "")
    summary = [f"{g}/  <- {len(n)} file(s): {', '.join(n[:3])}{', ...' if len(n) > 3 else ''}" for g, n in sorted(plan.items())]
    detail = (f"{count} loose file(s) in {root} would go into {len(plan)} folder(s), sorted by {by}. Nothing is deleted or "
              "renamed, folders already there are left alone, and undo puts everything back.\n" + "\n".join(summary)
              + (f"\n{len(skipped)} file(s) skipped because the same name is already in the folder." if skipped else ""))
    if not ctx.ask(f"Tidy {root} into {len(plan)} folder(s)?", detail, risky=False, where=root):
        return "DENIED: the user said no. Nothing was changed."
    items: list[dict[str, Any]] = []
    for group, names in plan.items():
        folder = os.path.join(root, group)
        if os.path.exists(folder) and not os.path.isdir(folder):
            continue
        if not os.path.isdir(folder):
            os.makedirs(folder)
            items.append({"op": "create", "path": folder})
        for name in names:
            src, dst = os.path.join(root, name), os.path.join(folder, name)
            shutil.move(src, dst)
            items.append({"op": "move", "src": src, "dst": dst})
    _batch(ctx, items)
    moved = sum(i["op"] == "move" for i in items) if len(items) > 1 else 1
    return f"OK: sorted {moved} file(s) in {root} into {len(plan)} folder(s) by {by}: " + "; ".join(f"{g} ({len(n)})" for g, n in sorted(plan.items()))


# ---------------------------------------------------------------- 4. bulk rename

_TOKEN = re.compile(r"\{(n|name|date)(?::0?(\d+))?\}")


def _renamed(stem: str, e: str, n: int, mtime: float, a: dict[str, Any]) -> str:
    old = str(a.get("find", ""))
    if old:
        stem = stem.replace(old, str(a.get("replace_with", "")))
    template = str(a.get("template", ""))
    if template:
        def sub(m: re.Match[str]) -> str:
            if m.group(1) == "n":
                return str(n).zfill(int(m.group(2) or 0))
            return stem if m.group(1) == "name" else time.strftime("%Y-%m-%d", time.localtime(mtime))
        stem = _TOKEN.sub(sub, template)
    stem = f"{a.get('prefix', '')}{stem}{a.get('suffix', '')}"
    case = str(a.get("case", "")).lower()
    stem = {"lower": stem.lower(), "upper": stem.upper(), "title": stem.title(), "sentence": stem[:1].upper() + stem[1:].lower()}.get(case, stem)
    return stem.strip() + e


def bulk_rename(ctx: Ctx, a: dict[str, Any]) -> str:
    root = _folder(ctx, a.get("folder"))
    ctx.access.check_writable(root)
    kind = normalize_kind(str(a.get("kind", "")))
    if kind is None:
        raise ToolError("kind must be excel, word, pdf, image, ... (or leave it out)")
    pattern = str(a.get("pattern", "")).strip() or "*"
    if not any(a.get(k) for k in ("find", "template", "prefix", "suffix", "case")):
        raise ToolError("say how to rename: find + replace_with, a template like 'Trip {n:02}', a prefix, a suffix or case")
    import fnmatch
    files = [n for n in os.listdir(root) if os.path.isfile(os.path.join(root, n)) and not os.path.islink(os.path.join(root, n))
             and not n.startswith(".") and fnmatch.fnmatch(n.lower(), pattern.lower())
             and (not kind or kind_of(n) == kind) and not ctx.access.is_blocked(os.path.join(root, n))]
    files.sort(key=lambda n: (os.path.getmtime(os.path.join(root, n)), n))
    start = int(a.get("start", 1))
    plan: list[tuple[str, str]] = []
    for i, name in enumerate(files):
        stem, e = os.path.splitext(name)
        new = _renamed(stem, e, start + i, os.path.getmtime(os.path.join(root, name)), a)
        if new != name:
            plan.append((name, _clean_name(new)))
    if not plan:
        return f"Nothing to rename: no file in {root} would get a different name."
    new_names = [n.lower() for _, n in plan]
    if len(set(new_names)) != len(new_names):
        raise ToolError("two files would get the same new name; use {n} in the template to number them")
    olds = {o.lower() for o, _ in plan}
    clash = [n for _, n in plan if os.path.lexists(os.path.join(root, n)) and n.lower() not in olds]
    if clash:
        raise ToolError(f"a file called '{clash[0]}' already exists in {root}; Hearth never overwrites. Choose other names")
    detail = f"{len(plan)} file(s) in {root}:\n" + _lines([f"{o}  ->  {n}" for o, n in plan], 15) + "\nUndo puts every name back."
    if not ctx.ask(f"Rename {len(plan)} file(s) in {root}?", detail, risky=False, where=root):
        return "DENIED: the user said no. Nothing was changed."
    items: list[dict[str, Any]] = []
    moves = [(os.path.join(root, o), os.path.join(root, n)) for o, n in plan]
    temps: list[tuple[str, str]] = []
    for src, dst in moves:                      # two steps so that swapped or shifted names cannot collide
        tmp = src + f".hearth-{len(temps)}"
        os.rename(src, tmp)
        temps.append((tmp, dst))
    for (tmp, dst), (src, _) in zip(temps, moves, strict=True):
        os.rename(tmp, dst)
        items.append({"op": "move", "src": src, "dst": dst})
    _batch(ctx, items)
    return f"OK: renamed {len(plan)} file(s) in {root}: " + "; ".join(f"{o} -> {n}" for o, n in plan[:6]) + ("; ..." if len(plan) > 6 else "")


# ---------------------------------------------------------------- 5. PDF tools

def _need_pypdf() -> Any:
    if importlib.util.find_spec("pypdf") is None:
        raise ToolError("this needs the pypdf package: run  python -m pip install pypdf  and ask again")
    import pypdf
    return pypdf


def _write_pdf(writer: Any, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".hearth-tmp"
    try:
        with open(tmp, "wb") as f:
            writer.write(f)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def pdf_merge(ctx: Ctx, a: dict[str, Any]) -> str:
    pypdf = _need_pypdf()
    names = _list(str(a["sources"]))
    if len(names) < 2:
        raise ToolError("give two or more PDFs in sources, separated by ; or one per line, in the order they should join")
    srcs = [_src(ctx, n, (".pdf",), a.get("destination")) for n in names]
    writer, counts = pypdf.PdfWriter(), []
    for s in srcs:
        if readers.ext(s) != ".pdf":
            raise ToolError(f"{os.path.basename(s)} is not a PDF")
        try:
            reader = pypdf.PdfReader(s)
            if reader.is_encrypted:
                raise ToolError(f"{os.path.basename(s)} is password protected")
            writer.append(reader)
            counts.append(len(reader.pages))
        except (pypdf.errors.PyPdfError, OSError, ValueError) as e:
            raise ToolError(f"cannot read {os.path.basename(s)}: {e}") from e
    dst = _new_file(ctx, a.get("destination"), a.get("name"), os.path.dirname(srcs[0]), ".pdf", "merged")
    detail = "In this order:\n" + _lines([f"{i}. {os.path.basename(s)} ({n} page(s))" for i, (s, n) in enumerate(zip(srcs, counts, strict=True), start=1)])
    if not ctx.ask(f"Create {dst} from {len(srcs)} PDFs ({sum(counts)} pages)?", detail, risky=False, where=dst):
        return "DENIED: the user said no. Nothing was changed."
    _write_pdf(writer, dst)
    _finish_write(ctx, dst, False, None)
    return f"OK: created {dst} with {sum(counts)} pages from {len(srcs)} PDFs"


def pdf_extract(ctx: Ctx, a: dict[str, Any]) -> str:
    pypdf = _need_pypdf()
    src = read_path(ctx, a["source"], prefer=(".pdf",))
    if readers.ext(src) != ".pdf":
        raise ToolError(f"{os.path.basename(src)} is not a PDF")
    try:
        reader = pypdf.PdfReader(src)
        if reader.is_encrypted:
            raise ToolError("that PDF is password protected")
        total = len(reader.pages)
        pages = readers.parse_pages(str(a["pages"]), total)
    except (pypdf.errors.PyPdfError, OSError, ValueError, readers.UnreadableError) as e:
        raise ToolError(str(e)) from e
    spec = str(a["pages"]).strip().replace(" ", "")
    dst = _new_file(ctx, a.get("destination"), a.get("name"), os.path.dirname(src), ".pdf",
                    f"{os.path.splitext(os.path.basename(src))[0]} pages {spec}")
    if not ctx.ask(f"Create {dst} with {len(pages)} page(s) of {os.path.basename(src)}?", f"Pages {spec} of {total}; the original is untouched.",
                   risky=False, where=dst):
        return "DENIED: the user said no. Nothing was changed."
    writer = pypdf.PdfWriter()
    for n in pages:
        writer.add_page(reader.pages[n - 1])
    _write_pdf(writer, dst)
    _finish_write(ctx, dst, False, None)
    return f"OK: created {dst} with page(s) {spec} of {src} ({len(pages)} of {total} pages)"


def images_to_pdf(ctx: Ctx, a: dict[str, Any]) -> str:
    if importlib.util.find_spec("PIL") is None:
        raise ToolError("this needs the Pillow package: run  python -m pip install pillow  and ask again")
    from PIL import Image
    names = _list(str(a["sources"]))
    if not names:
        raise ToolError("give the image files in sources, in page order")
    srcs = [_src(ctx, n, None, a.get("destination")) for n in names]
    dst = _new_file(ctx, a.get("destination"), a.get("name"), os.path.dirname(srcs[0]), ".pdf", "images")
    if not ctx.ask(f"Create {dst} from {len(srcs)} image(s)?", "One image per page, in this order:\n" + _lines(
            [f"{i}. {os.path.basename(s)}" for i, s in enumerate(srcs, start=1)]), risky=False, where=dst):
        return "DENIED: the user said no. Nothing was changed."
    pages = []
    try:
        for s in srcs:
            im = Image.open(s)
            if im.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
                im = bg
            pages.append(im.convert("RGB"))
    except (OSError, ValueError) as e:
        raise ToolError(f"cannot open an image: {e}") from e
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".hearth-tmp"
    try:
        pages[0].save(tmp, "PDF", save_all=True, append_images=pages[1:], resolution=100.0)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    _finish_write(ctx, dst, False, None)
    return f"OK: created {dst} with {len(pages)} page(s), one per image"


# ---------------------------------------------------------------- 6. fill a Word template

_PLACEHOLDER = re.compile(r"\{\{\s*([\w .-]+?)\s*\}\}|\{\s*([\w .-]+?)\s*\}|<<\s*([\w .-]+?)\s*>>|\[\s*([\w .-]+?)\s*\]")


def _values(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except ValueError:
            pass
    out: dict[str, str] = {}
    for part in re.split(r"[\n;]+", raw):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def fill_template(ctx: Ctx, a: dict[str, Any]) -> str:
    src = read_path(ctx, a["source"], prefer=(".docx",))
    if readers.ext(src) != ".docx":
        raise ToolError("fill_template works on Word (.docx) templates")
    try:
        xml = docxio.load(src)
    except docxio.DocxError as e:
        raise ToolError(str(e)) from e
    found: list[str] = []
    for line in docxio.paragraph_texts(xml):
        for m in _PLACEHOLDER.finditer(line):
            k = next(g for g in m.groups() if g)
            if k not in found:
                found.append(k)
    values = _values(str(a.get("values", "")))
    if not values:
        raise ToolError("no values given. Placeholders in this template: " + (", ".join(found) or "none found")
                        + ". Ask the user for their values, then pass values like name=Asha; date=12 Nov 2026")
    updated, total, used = xml, 0, []
    for key, val in values.items():
        for form in ("{{%s}}", "{%s}", "<<%s>>", "[%s]"):
            token = form % key
            if token in "".join(docxio.paragraph_texts(updated)):
                updated, n = docxio.replace(updated, token, val, True)
                total += n
                used.append(key)
    if not total:
        raise ToolError("none of those names is in the template. Placeholders there: " + (", ".join(found) or "none found"))
    dst = _new_file(ctx, a.get("destination"), a.get("name"), os.path.dirname(src), ".docx", os.path.splitext(os.path.basename(src))[0] + " filled")
    missing = [k for k in found if k not in values]
    detail = _lines([f"{k} -> {v}" for k, v in values.items() if k in used]) + (f"\nNot filled (no value given): {', '.join(missing)}" if missing else "")
    if not ctx.ask(f"Create {dst} from the template {os.path.basename(src)}?", detail, risky=False, where=dst):
        return "DENIED: the user said no. Nothing was changed."
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    docxio.save(dst, updated)
    _finish_write(ctx, dst, False, None)
    return (f"OK: created {dst} from {src} with {total} placeholder(s) filled; fonts and layout are the template's. The template is unchanged."
            + (f" Not filled: {', '.join(missing)}." if missing else "") + " (Headers and footers are not filled.)")


# ---------------------------------------------------------------- 7. new spreadsheet

_TBL = re.compile(r"<w:tbl>.*?</w:tbl>", re.S)
_ROW = re.compile(r"<w:tr[ >].*?</w:tr>", re.S)
_CELL = re.compile(r"<w:tc>.*?</w:tc>", re.S)


def _docx_tables(path: str) -> list[list[list[str]]]:
    xml = docxio.load(path)
    out = []
    for t in _TBL.findall(xml):
        out.append([[" ".join(docxio.paragraph_texts(c)).strip() for c in _CELL.findall(r)] for r in _ROW.findall(t)])
    return out


def _pdf_tables(path: str, pages: list[int] | None) -> list[list[list[str]]]:
    if importlib.util.find_spec("pdfplumber") is None:
        raise ToolError("reading tables from a PDF needs the pdfplumber package: run  python -m pip install pdfplumber  and ask again")
    import pdfplumber
    out: list[list[list[str]]] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            if pages and i not in pages:
                continue
            for t in page.extract_tables():
                out.append([[(c or "").replace("\n", " ").strip() for c in row] for row in t])
    return out


def _typed_rows(text: str) -> list[list[str]]:
    import csv
    import io
    text = text.strip("\n")
    delim = "\t" if "\t" in text else ";" if text.count(";") > text.count(",") else ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delim)]


def create_spreadsheet(ctx: Ctx, a: dict[str, Any]) -> str:
    rows_text, source = str(a.get("rows", "")), str(a.get("source", "")).strip()
    if not rows_text.strip() and not source:
        raise ToolError("give a source file (.csv, a Word or PDF with a table, another spreadsheet) or the rows as text")
    book: list[tuple[str, list[list[str]]]] = []
    where = "the typed rows"
    where_dir = default_scope(ctx)[0]
    if rows_text.strip():
        book = [("Sheet1", _typed_rows(rows_text))]
    else:
        src = read_path(ctx, source, prefer=(".csv", ".docx", ".pdf", ".xlsx", ".tsv"))
        e, where, where_dir = readers.ext(src), os.path.basename(src), os.path.dirname(src)
        try:
            if e in (".csv", ".tsv", ".xlsx", ".xlsm"):
                shs = sheets.load(src)
                want = str(a.get("sheet", "")).strip().lower()
                book = [(s.name, [[s.get(r, c) for c in range(1, s.max_col + 1)] for r in range(1, (max(s.rows) if s.rows else 0) + 1)])
                        for s in shs if not want or s.name.lower() == want]
            elif e == ".docx":
                book = [(f"Table {i}", t) for i, t in enumerate(_docx_tables(src), start=1)]
            elif e == ".pdf":
                pg = readers.parse_pages(str(a["pages"]), 10_000) if a.get("pages") else None
                book = [(f"Table {i}", t) for i, t in enumerate(_pdf_tables(src, pg), start=1)]
            else:
                raise ToolError(f"cannot make a spreadsheet from a {e or 'this'} file; use .csv, .docx, .pdf or .xlsx")
        except (docxio.DocxError, sheets.SheetError, readers.UnreadableError) as err:
            raise ToolError(str(err)) from err
        book = [(n, r) for n, r in book if r]
        if not book:
            raise ToolError(f"no table was found in {where}")
        if a.get("table"):
            idx = int(a["table"])
            if not 1 <= idx <= len(book):
                raise ToolError(f"{where} has {len(book)} table(s); table must be 1 to {len(book)}")
            book = [book[idx - 1]]
    dst = _new_file(ctx, a.get("destination"), a.get("name"), where_dir, ".xlsx", "table")
    cells = sum(len(r) for _, rows in book for r in rows)
    preview = "\n".join(" | ".join(r[:6]) for r in book[0][1][:5])
    if not ctx.ask(f"Create the spreadsheet {dst}?", f"{len(book)} sheet(s), {cells} cell(s) from {where}. First rows:\n{preview}", risky=False, where=dst):
        return "DENIED: the user said no. Nothing was changed."
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    sheets.create_xlsx(dst, book)
    _finish_write(ctx, dst, False, None)
    return f"OK: created {dst} with {len(book)} sheet(s) and {cells} cell(s) from {where}; the first row is bold and numbers are real numbers"


# ---------------------------------------------------------------- 8. zip files

_MAX_UNZIPPED = 2_000_000_000
_MAX_MEMBERS = 5000


def _members(z: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return [i for i in z.infolist() if not i.is_dir()]


def _safe_member(name: str) -> str:
    n = name.replace("\\", "/")
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in parts or not parts:
        raise ToolError(f"the archive holds an unsafe path ('{name}'); nothing was extracted")
    return os.path.join(*parts)


def _member_text(z: zipfile.ZipFile, info: zipfile.ZipInfo) -> str | None:
    """Readable text of one member, or None when it is not text (an image, a program...)."""
    e = ext(info.filename)
    if info.file_size > 20_000_000:
        return None
    if e in (".docx", ".xlsx", ".xlsm", ".pdf") or readers.indexable(info.filename):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "m" + e)
            with z.open(info) as src, open(p, "wb") as out:
                shutil.copyfileobj(src, out)
            try:
                return readers.read_text(p)
            except readers.UnreadableError:
                return None
    return None


def zip_contents(ctx: Ctx, a: dict[str, Any]) -> str:
    p = read_path(ctx, a["path"])
    if not zipfile.is_zipfile(p):
        raise ToolError(f"{os.path.basename(p)} is not a .zip archive (7z, rar and tar are not supported)")
    member, query = str(a.get("member", "")).strip(), str(a.get("query", "")).strip()
    try:
        with zipfile.ZipFile(p) as z:
            infos = _members(z)
            if member:
                hit = [i for i in infos if i.filename.lower() == member.lower().replace("\\", "/")
                       or os.path.basename(i.filename).lower() == member.lower()]
                if len(hit) != 1:
                    raise ToolError(f"'{member}' " + ("matches several files inside; give the full path inside the archive" if hit
                                                      else "is not inside the archive; call zip_contents without member to list"))
                text = _member_text(z, hit[0])
                if text is None:
                    raise ToolError(f"{hit[0].filename} is not a text, Word, Excel or PDF file, so it cannot be read")
                return f"{hit[0].filename} (inside {os.path.basename(p)}):\n{text[:5000]}" + ("\n[cut]" if len(text) > 5000 else "")
            if query:
                found: list[str] = []
                for info in infos[:300]:
                    text = _member_text(z, info)
                    if text and query.lower() in text.lower():
                        i = text.lower().index(query.lower())
                        found.append(f"{info.filename}\n    ...{text[max(0, i - 60): i + 100].strip()}...".replace("\n    ...", "\n    ...", 1))
                if not found:
                    return f"'{query}' was not found in the text files inside {p}." + (" (Only the first 300 files were searched.)" if len(infos) > 300 else "")
                return f"'{query}' appears in {len(found)} file(s) inside {p}:\n" + _lines(found, 20)
            total = sum(i.file_size for i in infos)
            listing = [f"{i.filename}  ({_size(i.file_size)})" for i in infos[:60]]
            return (f"{os.path.basename(p)}: {len(infos)} file(s), {_size(total)} unzipped.\n" + "\n".join(listing)
                    + (f"\n... {len(infos) - 60} more" if len(infos) > 60 else ""))
    except zipfile.BadZipFile as e:
        raise ToolError(f"the archive is damaged: {e}") from e


def extract_zip(ctx: Ctx, a: dict[str, Any]) -> str:
    src = read_path(ctx, a["source"], prefer=(".zip",))
    if not zipfile.is_zipfile(src):
        raise ToolError(f"{os.path.basename(src)} is not a .zip archive")
    wanted = {m.lower().replace("\\", "/") for m in _list(str(a.get("members", "")))}
    try:
        with zipfile.ZipFile(src) as z:
            infos = [i for i in _members(z) if not wanted or i.filename.lower() in wanted or os.path.basename(i.filename).lower() in wanted]
            if not infos:
                raise ToolError("no file in the archive matches")
            total = sum(i.file_size for i in infos)
            packed = sum(i.compress_size for i in infos) or 1
            if len(infos) > _MAX_MEMBERS or total > _MAX_UNZIPPED or (total > 50_000_000 and total / packed > 200):
                raise ToolError("this archive is too big or too strongly compressed to extract safely")
            for i in infos:
                _safe_member(i.filename)
                if (i.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ToolError(f"the archive holds a link ('{i.filename}'); nothing was extracted")
            folder = _new_file(ctx, a.get("destination"), a.get("name"), os.path.dirname(src), "", os.path.splitext(os.path.basename(src))[0])
            names = [i.filename for i in infos]
            detail = f"{len(infos)} file(s), {_size(total)}, go into the new folder {folder}.\n" + _lines(names)
            if not ctx.ask(f"Extract {os.path.basename(src)} into {folder}?", detail, risky=False, where=folder):
                return "DENIED: the user said no. Nothing was changed."
            os.makedirs(folder)
            try:
                for i in infos:
                    out = os.path.join(folder, _safe_member(i.filename))
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with z.open(i) as s, open(out, "wb") as d:
                        shutil.copyfileobj(s, d)
            except BaseException:
                shutil.rmtree(folder, ignore_errors=True)
                raise
    except zipfile.BadZipFile as e:
        raise ToolError(f"the archive is damaged: {e}") from e
    ctx.journal.record("create", path=folder)
    ctx.index.sync()
    return f"OK: extracted {len(infos)} file(s) ({_size(total)}) from {src} into the new folder {folder}"


# ---------------------------------------------------------------- 9. history and undo

def _describe(e: dict[str, Any]) -> str:
    op = e.get("op")
    def base(p: Any) -> str:
        return os.path.basename(str(p).rstrip("\\/")) or str(p)
    if op == "create":
        return f"created {e['path']}"
    if op == "copy":
        return f"copied to {e['path']}"
    if op == "edit":
        return f"edited {e['path']}"
    if op == "move":
        return f"moved or renamed {base(e['src'])} -> {e['dst']}"
    if op == "delete":
        return f"deleted {e['path']}"
    if op == "batch":
        items = e.get("items", [])
        moved = [i for i in items if i.get("op") == "move"]
        return f"changed {len(moved) or len(items)} file(s) at once" + (f", for example {base(moved[0]['src'])} -> {moved[0]['dst']}" if moved else "")
    return str(op)


def show_history(ctx: Ctx, a: dict[str, Any]) -> str:
    n = max(1, min(int(a.get("limit", 10)), 40))
    items = ctx.journal.recent(n * 5 if a.get("today") else n)
    if a.get("today"):
        start = time.mktime(dt.date.today().timetuple())
        items = [e for e in items if e.get("ts", 0) >= start][:n]
    if not items:
        return "Hearth has not changed anything yet" + (" today." if a.get("today") else ".")
    nxt = ctx.journal.next_undo()
    lines = [f"{_when(e['ts'])}  {_describe(e)}" + ("  (undone)" if e.get("undone") else "") + ("  <- undo would reverse this" if nxt and e["id"] == nxt["id"] else "")
             for e in items]
    return "Changes Hearth made, newest first:\n" + "\n".join(lines)


def undo_change(ctx: Ctx, a: dict[str, Any]) -> str:
    from .journal import UndoError
    nxt = ctx.journal.next_undo()
    if nxt is None:
        return "There is nothing to undo."
    if not ctx.ask(f"Undo: {_describe(nxt)}?", "Hearth will put things back the way they were before that change.", risky=False):
        return "DENIED: the user said no. Nothing was changed."
    try:
        msg = ctx.journal.undo()
    except UndoError as e:
        raise ToolError(str(e)) from e
    ctx.index.sync()
    return f"OK: {msg}"


# ---------------------------------------------------------------- 10. dates and deadlines in documents

_MONTHS = {m: i for i, names in enumerate(
    ["jan january", "feb february", "mar march", "apr april", "may", "jun june", "jul july", "aug august", "sep sept september",
     "oct october", "nov november", "dec december"], start=1) for m in names.split()}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_D_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_D_NUM = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b")
_D_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}\b\.?,?(?:\s+(\d{{4}}))?", re.I)
_D_MDY = re.compile(rf"\b{_MON}\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b,?(?:\s+(\d{{4}}))?", re.I)
_WORDS = re.compile(r"\b(deadline|due|expires?|expiry|expiration|renew\w*|submit\w*|last date|by|before|payable|payment|exam|interview|"
                    r"appointment|meeting|due date|valid (?:until|till))\b", re.I)


def _dates(text: str, today: dt.date) -> list[tuple[dt.date, int]]:
    """(date, position) for every date written in the text. dd/mm/yyyy is read day first. A date with no year is this year."""
    out: list[tuple[dt.date, int]] = []

    def add(y: int, m: int, d: int, pos: int) -> None:
        with contextlib.suppress(ValueError):
            out.append((dt.date(y, m, d), pos))
    for m in _D_ISO.finditer(text):
        add(int(m[1]), int(m[2]), int(m[3]), m.start())
    for m in _D_NUM.finditer(text):
        add(int(m[3]), int(m[2]), int(m[1]), m.start())
    for m in _D_DMY.finditer(text):
        add(int(m[3] or today.year), _MONTHS[m[2].lower()], int(m[1]), m.start())
    for m in _D_MDY.finditer(text):
        add(int(m[3] or today.year), _MONTHS[m[1].lower()], int(m[2]), m.start())
    return out


def find_deadlines(ctx: Ctx, a: dict[str, Any]) -> str:
    days = max(1, min(int(a.get("days", 30)), 730))
    today = dt.date.today()
    lo, hi = (today - dt.timedelta(days=days) if a.get("past") else today), today + dt.timedelta(days=days)
    kind = normalize_kind(str(a.get("kind", "")))
    if kind is None:
        raise ToolError("kind must be excel, word, pdf, ... (or leave it out)")
    if a.get("folder"):
        folder = str(ctx.access.resolve(a["folder"]))
        need_read(ctx, folder)
        roots = [folder]
    else:
        roots = ctx.access.readable_roots()
        if not roots:
            raise ToolError("no folder is open for reading yet. Call find_deadlines again with folder=<a folder> and the user will be asked")
    budget = Budget(seconds=20.0, files=5000)
    hits: list[tuple[dt.date, bool, str, str]] = []
    scanned = 0
    for root in roots:
        for dp, _, files in walk(ctx, root, budget):
            for f in files:
                p = os.path.join(dp, f)
                if not readers.indexable(p) or (kind and kind_of(p) != kind) or scanned >= 400:
                    continue
                try:
                    if os.path.getsize(p) > 20_000_000:
                        continue
                    text = readers.read_text(p, 2_000_000)
                except (readers.UnreadableError, OSError, sheets.SheetError):
                    continue
                scanned += 1
                seen: set[tuple[dt.date, int]] = set()
                for d, pos in _dates(text, today):
                    if not lo <= d <= hi or (d, pos // 80) in seen:
                        continue
                    seen.add((d, pos // 80))
                    ctx_text = re.sub(r"\s+", " ", text[max(0, pos - 70): pos + 90]).strip()
                    hits.append((d, bool(_WORDS.search(ctx_text)), p, ctx_text))
    if not hits:
        return (f"No dates between {lo} and {hi} were found in {scanned} document(s) under {' and '.join(roots)}."
                + (" (The scan stopped at its limit.)" if budget.truncated or scanned >= 400 else ""))
    hits.sort(key=lambda h: (h[0], not h[1], h[2]))
    ctx.found = list(dict.fromkeys(h[2] for h in hits))[:100]
    body = "\n".join(f"{d.strftime('%a %d %b %Y')}{'  [deadline word]' if w else ''}  {p}\n    {c}" for d, w, p, c in hits[:30])
    return (f"{len(hits)} date(s) between {lo} and {hi} in {scanned} document(s). Numbers like 05/11/2026 are read as day/month/year.\n"
            + body + (f"\n... {len(hits) - 30} more" if len(hits) > 30 else "") + ("\n(The scan stopped at its limit.)" if budget.truncated or scanned >= 400 else ""))


# ---------------------------------------------------------------- registry

TOOLS.update({t.name: t for t in [
    Tool("compare_files", "show what is different between two files (text, Word, PDF, or spreadsheet cells); changes nothing",
         [_P("first", str, "the first (older) file"), _P("second", str, "the second (newer) file")], False, compare_files),
    Tool("find_duplicates", "find files with exactly the same content in a folder and everything under it; changes nothing",
         [_P("folder", str, "folder to scan (default: the chosen folder)", False), _P("kind", str, "limit to excel, word, pdf, image...", False),
          _P("min_kb", int, "ignore files smaller than this many KB", False)], False, find_duplicates),
    Tool("move_duplicates", "move the extra copies of identical files into a _duplicates folder (the oldest copy stays); nothing is deleted",
         [_P("folder", str, "folder to clean", False), _P("kind", str, "limit to one kind", False),
          _P("min_kb", int, "ignore files smaller than this many KB", False)], True, move_duplicates),
    Tool("tidy_folder", "sort the loose files of a folder into subfolders by type (Word documents, PDFs, Images...), by date (2026-11) or by extension",
         [_P("folder", str, "folder to tidy (default: the chosen folder)", False), _P("by", str, "type (default), date or extension", False)],
         True, tidy_folder),
    Tool("bulk_rename", "rename many files in a folder at once: find+replace_with, a template like 'Trip {n:02}' ({n} number, {name} old name, "
         "{date} modified date), prefix, suffix or case (lower/upper/title)",
         [_P("folder", str, "folder (default: the chosen folder)", False), _P("pattern", str, "which files, e.g. *.jpg (default all)", False), _P("kind", str, "limit to one kind", False),
          _P("find", str, "text to replace in the names", False), _P("replace_with", str, "what to put instead", False),
          _P("template", str, "new name pattern, extension is kept", False), _P("start", int, "first number for {n} (default 1)", False),
          _P("prefix", str, "text to add before the name", False), _P("suffix", str, "text to add after the name", False),
          _P("case", str, "lower, upper, title or sentence", False)], True, bulk_rename),
    Tool("pdf_merge", "join several PDFs into one new PDF, in the order given",
         [_P("sources", str, "PDF names separated by ; or one per line"), _P("destination", str, "folder or drive for the result", False),
          _P("name", str, "name for the new PDF", False)], True, pdf_merge),
    Tool("pdf_extract", "make a new PDF from some pages of a PDF (the original is untouched)",
         [_P("source", str, "the PDF"), _P("pages", str, "e.g. 1-3 or 2,5"), _P("destination", str, "folder or drive", False),
          _P("name", str, "name for the new PDF", False)], True, pdf_extract),
    Tool("images_to_pdf", "make a PDF with one image per page",
         [_P("sources", str, "image files in page order, separated by ; or one per line"), _P("destination", str, "folder or drive", False),
          _P("name", str, "name for the new PDF", False)], True, images_to_pdf),
    Tool("fill_template", "make a copy of a Word template with its {placeholders} filled in; formatting is kept, the template is untouched",
         [_P("source", str, "the Word template"), _P("values", str, "name=Asha; date=12 Nov 2026 (or a JSON object)", False),
          _P("destination", str, "folder or drive", False), _P("name", str, "name for the filled copy", False)], True, fill_template),
    Tool("create_spreadsheet", "make a new .xlsx from a .csv, a table in a Word or PDF file, another spreadsheet, or typed rows",
         [_P("source", str, "file with the data", False), _P("rows", str, "typed rows: one per line, cells separated by commas", False),
          _P("table", int, "which table of the file, 1 = first", False), _P("pages", str, "PDF pages to look in", False),
          _P("sheet", str, "sheet to copy from a spreadsheet", False), _P("destination", str, "folder or drive", False),
          _P("name", str, "name for the new spreadsheet", False)], True, create_spreadsheet),
    Tool("zip_contents", "look inside a .zip: list its files (default), read one file (member) or search the text inside (query)",
         [_P("path", str, "the .zip"), _P("member", str, "a file inside to read", False), _P("query", str, "words to search for inside", False)],
         False, zip_contents),
    Tool("extract_zip", "unpack a .zip (all, or just members) into a NEW folder; never overwrites",
         [_P("source", str, "the .zip"), _P("destination", str, "folder or drive for the new folder", False),
          _P("name", str, "name of the new folder", False), _P("members", str, "only these files, separated by ;", False)], True, extract_zip),
    Tool("show_history", "list the changes Hearth made (newest first), and which one undo would reverse",
         [_P("limit", int, "how many (default 10)", False), _P("today", bool, "only today's", False)], False, show_history),
    Tool("undo_change", "undo the most recent change Hearth made (a batch of files is undone together)", [], True, undo_change),
    Tool("find_deadlines", "find dates (deadlines, due dates, renewals) written inside documents, within the next N days; reads text only",
         [_P("days", int, "how many days ahead (default 30)", False), _P("folder", str, "where to look (asks permission if needed)", False),
          _P("kind", str, "limit to word, pdf, excel...", False), _P("past", bool, "also the same number of days back", False)], False, find_deadlines),
]})
