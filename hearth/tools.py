"""The actions the assistant can take. Read-only tools run freely; every change asks the user first and is undoable."""
from __future__ import annotations

import contextlib
import difflib
import fnmatch
import os
import re
import shutil
import time
from collections import Counter
from typing import Any

from . import docxio, finder, pdfout, pdfrich, readers, sheets
from .access import AccessError, dedupe
from .filetypes import KINDS, LABELS, ext, kind_of, normalize_kind
from .tools_base import Budget, Ctx, DeniedError, Param, Tool, ToolError, default_scope, need_read, read_path, walk

__all__ = ["Ctx", "Param", "TOOLS", "Tool", "ToolError", "run_tool"]

MAX_OBS = 6000          # characters of any single tool result handed back to the model
READ_WINDOW = 5000      # characters returned per read_file call
MAX_WRITE = 1_000_000
OPENABLE = {".txt", ".md", ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".csv", ".png", ".jpg", ".jpeg", ".gif",
            ".bmp", ".webp", ".svg", ".mp3", ".wav", ".mp4", ".mov", ".mkv", ".json", ".rtf", ".odt", ".ods"}


# ---------------------------------------------------------------- helpers

def _size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _diff(old: str, new: str, name: str, limit: int = 30) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"before: {name}", f"after: {name}",
                                      lineterm="", n=1))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... ({len(lines) - limit} more diff lines)"]
    return "\n".join(lines) or "(no visible change)"


def _preview(text: str, limit: int = 20) -> str:
    lines = text.splitlines()
    shown = "\n".join(lines[:limit])
    return shown + (f"\n... ({len(lines) - limit} more lines)" if len(lines) > limit else "")


def _match_newlines(text: str, s: str) -> str:
    """If the file uses Windows line endings, make the model's text match them."""
    return s.replace("\r\n", "\n").replace("\n", "\r\n") if "\r\n" in text else s


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.hearth-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _need_editable(path: str, docx_ok: bool = False) -> None:
    if readers.editable(path) or (docx_ok and readers.ext(path) == ".docx"):
        return
    e = readers.ext(path)
    name = os.path.basename(path)
    if e in sheets.SHEET_EXTS | readers.SHEET_BOOK_EXTS:
        hint = "change one cell at a time with sheet_find and sheet_set_cell"
    elif e == ".docx":
        hint = "use replace_text or append_text to edit it, or copy_to_word to make or extend one"
    elif e == ".pdf":
        hint = "I can read a PDF but not edit it; I can write what you need to a new .txt/.md file"
    else:
        hint = "I can read it, or write the result to a new .txt/.md file"
    raise ToolError(f"'{name}' is a {e} file and this tool only handles plain-text files; {hint}")


# ---------------------------------------------------------------- read-only tools

def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _places(ctx: Ctx) -> str:
    return "\n".join(f"  {label}: {path}" for label, path in ctx.access.places())


def list_folder(ctx: Ctx, a: dict[str, Any]) -> str:
    raw = a.get("path", "")
    if not raw:
        tops = dedupe(ctx.access.trusted)
        if len(tops) != 1:
            return "Places on this computer:\n" + _places(ctx)
        raw = tops[0]                       # "the current folder" with one trusted folder = that folder
    p = str(ctx.access.resolve(raw))
    if not os.path.isdir(p):
        raise ToolError("that is a file, not a folder; use read_file")
    names = [n for n in os.listdir(p) if not ctx.access.is_blocked(os.path.join(p, n))]
    names.sort(key=lambda n: (not os.path.isdir(os.path.join(p, n)), n.lower()))
    lines = []
    for n in names[:100]:
        full = os.path.join(p, n)
        try:
            lines.append(f"  {n}/" if os.path.isdir(full) else f"  {n}  ({_size(os.path.getsize(full))})")
        except OSError:
            lines.append(f"  {n}  (unavailable)")
    more = f"\n  ... and {len(names) - 100} more" if len(names) > 100 else ""
    return f"{p}  ({len(names)} items)\n" + ("\n".join(lines) if lines else "  (empty)") + more


def folder_summary(ctx: Ctx, a: dict[str, Any]) -> str:
    """Counts of the files in a folder (and everything under it) by type, then the total. Names and sizes only."""
    raw = a.get("path", "")
    if raw:
        roots = [str(ctx.access.resolve(raw))]
        if not os.path.isdir(roots[0]):
            raise ToolError("that is a file, not a folder; use file_info")
    elif ctx.access.trusted:
        roots = dedupe(ctx.access.trusted)
    else:
        return ("No folder is chosen yet, so there is no 'current folder'. Places on this computer:\n" + _places(ctx)
                + "\nAsk the user which folder they mean, or call folder_summary with one of these paths.")
    budget = Budget(10.0)
    by_kind: Counter[str] = Counter()
    by_ext: dict[str, Counter[str]] = {}
    total_size = folders = 0
    for root in roots:
        for dp, dns, fns in walk(ctx, root, budget):
            folders += len(dns)
            for fn in fns:
                if fn.startswith("~$"):
                    continue
                full = os.path.join(dp, fn)
                k = kind_of(fn)
                by_kind[k] += 1
                by_ext.setdefault(k, Counter())[ext(fn) or "no extension"] += 1
                with contextlib.suppress(OSError):
                    total_size += os.path.getsize(full)
    total = sum(by_kind.values())
    lines = []
    for k, n in by_kind.most_common():
        exts = ", ".join(f"{e} {c}" for e, c in by_ext[k].most_common(4))
        lines.append(f"  {LABELS.get(k, k)}: {n}  ({exts})")
    head = f"Files under {' and '.join(roots)}: {folders} sub-folder(s), {_size(total_size)}"
    cut = "\n(This is only part of the folder: the scan stopped at its time/size limit, so the real total is higher.)" \
        if budget.truncated else ""
    return head + "\n" + ("\n".join(lines) if lines else "  (no files)") + f"\nTotal: {total} file(s){cut}"


def find_files(ctx: Ctx, a: dict[str, Any]) -> str:
    """Find files by name and/or type anywhere. Metadata only: nothing is opened, so no permission is needed."""
    pattern = str(a.get("pattern", "")).strip().lower()
    kind = normalize_kind(str(a.get("kind", "")))
    if kind is None:
        raise ToolError(f"kind must be one of: {', '.join(KINDS)} (or leave it out)")
    if not pattern and not kind:
        raise ToolError("give a name pattern, a kind (excel, word, pdf, image, ...), or both")
    if pattern and not any(c in pattern for c in "*?["):
        pattern = f"*{pattern}*"
    starts = [str(ctx.access.resolve(a["folder"]))] if a.get("folder") else default_scope(ctx)
    exts = KINDS.get(kind) if kind else None
    budget = Budget(10.0)
    found: list[tuple[float, str, int]] = []
    for start in starts:
        for dp, dns, fns in walk(ctx, start, budget):
            for n in fns + ([] if kind else dns):
                if n.startswith("~$") or (pattern and not fnmatch.fnmatch(n.lower(), pattern)):
                    continue
                if exts is not None and ext(n) not in exts:
                    continue
                full = os.path.join(dp, n)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                found.append((st.st_mtime, full + ("/" if os.path.isdir(full) else ""), st.st_size))
    where = " and ".join(starts)
    if not found:
        return f"No matching files under {where}." + (" (The scan stopped at its limit.)" if budget.truncated else "")
    found.sort(key=lambda t: -t[0])
    cap = min(max(int(a.get("limit", 40)), 1), 200)
    ctx.found = [f.rstrip("/") for _, f, _ in found[:cap] if not f.endswith("/")]
    ctx.found_why = {f.rstrip("/"): "modified " + _when(m) for m, f, _ in found[:cap] if not f.endswith("/")}
    lines = [f"  {f}  ({_size(sz)}, {_when(m)})" if not f.endswith("/") else f"  {f}" for m, f, sz in found[:cap]]
    shown = f"The {cap} newest of {len(found)} matches" if len(found) > cap else f"{len(found)} match(es)"
    cut = " The scan stopped at its limit, so there may be more." if budget.truncated else ""
    return f"{shown}, newest first, under {where}:\n" + "\n".join(lines) + cut


def search_text(ctx: Ctx, a: dict[str, Any]) -> str:
    """Search INSIDE files. Reading content needs permission, so this covers trusted/allowed places; a `folder`
    outside them asks the user first."""
    query = str(a["query"]).strip()
    kind = normalize_kind(str(a.get("kind", "")))
    if kind is None:
        raise ToolError(f"kind must be one of: {', '.join(KINDS)} (or leave it out)")
    if a.get("folder"):
        folder = str(ctx.access.resolve(a["folder"]))
        if not os.path.isdir(folder):
            raise ToolError("that is a file, not a folder; use read_file")
        need_read(ctx, folder)
        roots = [folder]
        ctx.index.sync(roots)
    else:
        roots = ctx.access.readable_roots()
        if not roots:
            raise ToolError("no folder is open for reading yet. Call search_text again with folder=<a folder> and the "
                            "user will be asked; find_files can look at file names without asking")
    found, cut = finder.search(ctx, query, kind, roots)
    where = " and ".join(roots)
    if not found:
        return (f"No matches inside files under {where}. Try one different word, or another folder."
                + (" (The scan stopped at its limit.)" if cut else ""))
    ctx.found = [f.path for f in found]
    ctx.found_why = {f.path: f.why for f in found}
    body = "\n".join(f"{f.path}\n    {f.why}" for f in found)
    return body + f"\n({len(found)} file(s); searched under {where}. Name every file that answers the question, with its full path.)"


def read_file(ctx: Ctx, a: dict[str, Any]) -> str:
    p = read_path(ctx, a["path"])
    if os.path.isdir(p):
        raise ToolError("that is a folder; use list_folder")
    try:
        text = readers.read_text(p)
    except readers.UnreadableError as e:
        raise ToolError(str(e)) from e
    off = max(0, a.get("offset", 0))
    part = text[off:off + READ_WINDOW]
    if not part and off:
        return "(end of file)"
    if not text:
        return "(the file is empty)"
    tail = off + READ_WINDOW
    if tail < len(text):
        part += f"\n[truncated: {len(text) - tail} more characters; call read_file again with offset={tail}]"
    return part


def file_info(ctx: Ctx, a: dict[str, Any]) -> str:
    p = str(ctx.access.resolve(a["path"]))
    st = os.stat(p)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
    if os.path.isdir(p):
        return f"{p}\n  folder, {len(os.listdir(p))} items, modified {when}"
    return f"{p}\n  file, {_size(st.st_size)} ({st.st_size} bytes), modified {when}"


# ---------------------------------------------------------------- tools that change things

def _finish_write(ctx: Ctx, path: str, existed: bool, backup: str | None) -> None:
    if existed and backup:
        ctx.journal.record("edit", path=path, backup=backup)
    else:
        ctx.journal.record("create", path=path)
    ctx.index.refresh(path)


def _writable(ctx: Ctx, raw: str, must_exist: bool = True, read: bool = True) -> str:
    """Resolve a path that is about to change; also ask to read it first when it already exists."""
    p = str(ctx.access.resolve(raw, must_exist=must_exist))
    ctx.access.check_writable(p)
    if read and os.path.exists(p):
        need_read(ctx, p)
    return p


def write_file(ctx: Ctx, a: dict[str, Any]) -> str:
    p = _writable(ctx, a["path"], must_exist=False)
    content: str = a["content"]
    if len(content) > MAX_WRITE:
        raise ToolError("content is too large")
    _need_editable(p)
    if os.path.isdir(p):
        raise ToolError("that path is a folder")
    exists = os.path.exists(p)
    if exists and not a.get("overwrite", False):
        raise ToolError("that file already exists. To change part of it use replace_text or append_text; "
                        "to replace everything set overwrite=true")
    if exists:
        try:
            old = readers.read_text_strict(p)
        except readers.UnreadableError as e:
            raise ToolError(str(e)) from e
        ok = ctx.ask(f"Replace the contents of {p}?", _diff(old, content, os.path.basename(p)), risky=True)
    else:
        ok = ctx.ask(f"Create {p}?", _preview(content), risky=False, where=p)
    if not ok:
        return "DENIED: the user said no. Nothing was changed."
    backup = ctx.journal.backup(p) if exists else None
    os.makedirs(os.path.dirname(p), exist_ok=True)
    _atomic_write(p, content)
    _finish_write(ctx, p, exists, backup)
    return f"OK: {'replaced' if exists else 'created'} {p} ({len(content)} characters)"


def replace_text(ctx: Ctx, a: dict[str, Any]) -> str:
    p = _writable(ctx, a["path"])
    _need_editable(p, docx_ok=True)
    if os.path.isdir(p):
        raise ToolError("that path is a folder")
    if readers.ext(p) == ".docx":
        return _replace_docx(ctx, p, a)
    try:
        text = readers.read_text_strict(p)
    except readers.UnreadableError as e:
        raise ToolError(str(e)) from e
    old = _match_newlines(text, a["old"])
    new = _match_newlines(text, a["new"])
    if not old:
        raise ToolError("'old' must not be empty")
    n = text.count(old)
    if n == 0:
        raise ToolError("that text was not found. Call read_file and copy the exact text, including spacing")
    if n > 1 and not a.get("replace_all", False):
        raise ToolError(f"that text appears {n} times; include more surrounding text, or set replace_all=true")
    updated = text.replace(old, new)
    if not ctx.ask(f"Change {p}?", _diff(text, updated, os.path.basename(p)), risky=True):
        return "DENIED: the user said no. Nothing was changed."
    backup = ctx.journal.backup(p)
    _atomic_write(p, updated)
    _finish_write(ctx, p, True, backup)
    return f"OK: replaced {n if a.get('replace_all') else 1} occurrence(s) in {p}"


def _replace_docx(ctx: Ctx, p: str, a: dict[str, Any]) -> str:
    old, new = a["old"], a["new"]
    if not old:
        raise ToolError("'old' must not be empty")
    try:
        xml = docxio.load(p)
        n = docxio.count(xml, old)
        if n == 0:
            raise ToolError("that text was not found in the document. Call read_file and copy the exact words")
        if n > 1 and not a.get("replace_all", False):
            raise ToolError(f"that text appears {n} times; include more surrounding words, or set replace_all=true")
        updated, done = docxio.replace(xml, old, new, bool(a.get("replace_all", False)))
        if not ctx.ask(f"Change {p}?", docxio.diff(xml, updated), risky=True):
            return "DENIED: the user said no. Nothing was changed."
        backup = ctx.journal.backup(p)
        docxio.save(p, updated)
    except docxio.DocxError as e:
        raise ToolError(str(e)) from e
    _finish_write(ctx, p, True, backup)
    return f"OK: replaced {done} occurrence(s) in {p}; formatting of everything else is untouched"


def _append_docx(ctx: Ctx, p: str, add: str) -> str:
    if not os.path.exists(p):
        raise ToolError("I can add to an existing Word document but cannot create a new .docx; use a .txt or .md file")
    try:
        xml = docxio.load(p)
        updated = docxio.append(xml, add)
        if not ctx.ask(f"Add text to the end of {p}?", docxio.diff(xml, updated), risky=True):
            return "DENIED: the user said no. Nothing was changed."
        backup = ctx.journal.backup(p)
        docxio.save(p, updated)
    except docxio.DocxError as e:
        raise ToolError(str(e)) from e
    _finish_write(ctx, p, True, backup)
    return f"OK: added {len(add)} characters to the end of {p}"


def append_text(ctx: Ctx, a: dict[str, Any]) -> str:
    p = _writable(ctx, a["path"], must_exist=False)
    _need_editable(p, docx_ok=True)
    if os.path.isdir(p):
        raise ToolError("that path is a folder")
    add: str = a["text"]
    if readers.ext(p) == ".docx":
        return _append_docx(ctx, p, add)
    exists = os.path.exists(p)
    try:
        text = readers.read_text_strict(p) if exists else ""
    except readers.UnreadableError as e:
        raise ToolError(str(e)) from e
    if len(text) + len(add) > MAX_WRITE:
        raise ToolError("the file would become too large")
    sep = ("\r\n" if "\r\n" in text else "\n") if text and not text.endswith("\n") else ""
    updated = text + sep + _match_newlines(text, add)
    detail = _diff(text, updated, os.path.basename(p)) if exists else _preview(add)
    if not ctx.ask(f"Add text to {p}?" if exists else f"Create {p}?", detail, risky=exists, where=p):
        return "DENIED: the user said no. Nothing was changed."
    backup = ctx.journal.backup(p) if exists else None
    os.makedirs(os.path.dirname(p), exist_ok=True)
    _atomic_write(p, updated)
    _finish_write(ctx, p, exists, backup)
    return f"OK: added {len(add)} characters to {p}"


def _word_target(ctx: Ctx, raw: str, source: str | None, name: str | None = None) -> tuple[str, str]:
    """Where a new/extended Word document goes. A folder (e.g. Downloads) gets a file named after the source; an
    extension that is not .docx (.word, .doc, none) is corrected, because only .docx can be written. Returns (path, note)."""
    dst = str(ctx.access.resolve(raw, must_exist=False))
    ctx.access.check_writable(dst)
    note = ""
    if os.path.isdir(dst):
        if name and name.strip():
            stem = re.sub(r"\.(docx|doc|word|rtf|odt)$", "", name.strip(), flags=re.I)
            if not stem or re.search(r'[\\/:*?"<>|]', stem):
                raise ToolError(f"'{name}' cannot be used as a file name (no \\ / : * ? \" < > |)")
        else:
            stem = os.path.splitext(os.path.basename(source))[0] if source else "Hearth document"
        dst = os.path.join(dst, stem + ".docx")
    else:
        base, e = os.path.splitext(dst)
        if e.lower() != ".docx":
            if e.lower() in ("", ".word", ".doc", ".docs", ".wordx", ".rtf", ".odt"):
                dst = base + ".docx"
                note = " (Word documents are saved as .docx, so the name was changed)"
            else:
                raise ToolError(f"'{os.path.basename(dst)}' is not a Word document name. Use a .docx name, e.g. {base}.docx")
    ctx.access.check_writable(dst)
    return dst, note


def _find_column(sh: sheets.Sheet, ref: str) -> tuple[int, int]:
    """(header row, column). Real sheets often have a title above the header, so try the first few rows."""
    first: sheets.SheetError | None = None
    for hrow in sh.rows[:8]:
        try:
            return hrow, sheets.resolve_column(sh, ref, hrow)
        except sheets.SheetError as e:
            first = first or e
    raise first or sheets.SheetError("the sheet is empty")


def _source_paragraphs(ctx: Ctx, a: dict[str, Any]) -> tuple[list[str], str, pdfrich.Rich | None, str]:
    """The content to copy: typed text, a spreadsheet column, PDF pages, or a whole file. Returns (paragraphs, what)."""
    paras: list[str] = []
    what = ""
    rich: pdfrich.Rich | None = None
    why = ""   # why the formatting could not be kept, when that is so
    if a.get("text"):
        paras += str(a["text"]).split("\n")
        what = "your text"
    if a.get("source"):
        src = str(ctx.access.resolve(a["source"]))
        if os.path.isdir(src):
            raise ToolError("the source is a folder, not a file")
        need_read(ctx, src)
        e = readers.ext(src)
        name = os.path.basename(src)
        if a.get("pages") and e != ".pdf":
            raise ToolError("'pages' only applies to PDF files")
        if a.get("column") and e not in sheets.SHEET_EXTS:
            raise ToolError("'column' only applies to spreadsheets (.xlsx, .csv, .tsv)")
        try:
            if e in sheets.SHEET_EXTS:
                if not a.get("column"):
                    raise ToolError("which column? Pass column=<header or letter>; sheet_info shows the columns")
                books = _books(src)
                sh = _pick(ctx, src, books, a.get("sheet"))
                hrow, col = _find_column(sh, str(a["column"]))
                head = sh.headers(hrow).get(col, sheets.col_letters(col))
                paras += [head] + [sh.get(r, col) for r in sh.rows if r > hrow]
                what = f"the '{head}' column of {name} ({max(len(paras) - 1, 0)} values, header included)"
            elif e == ".pdf":
                pages = readers.pdf_pages(src)
                chosen = readers.parse_pages(str(a.get("pages", "all")), len(pages))
                for n in chosen:
                    paras += pages[n - 1].split("\n") + [""]
                what = f"page(s) {chosen[0]}-{chosen[-1]} of {name}" if len(chosen) > 1 else f"page {chosen[0]} of {name}"
                if str(a.get("pages", "")).strip() and chosen[-1] < _hi(str(a["pages"])):
                    what += f" (the PDF has only {len(pages)} pages)"
                if a.get("plain") or a.get("text"):
                    why = "plain text was asked for" if a.get("plain") else ""
                else:
                    try:
                        rich = pdfrich.extract(src, chosen)
                    except pdfrich.RichError as err:
                        why = str(err)
            else:
                paras += readers.read_text(src).split("\n")
                what = f"the text of {name}"
        except (sheets.SheetError, readers.UnreadableError) as err:
            raise ToolError(str(err)) from err
    if not paras:
        raise ToolError("nothing to copy: give source (a file) and/or text")
    return paras, what, rich, why


def _hi(spec: str) -> int:
    nums = [int(n) for n in re.findall(r"\d+", spec)]
    return max(nums) if nums else 0


def copy_to_word(ctx: Ctx, a: dict[str, Any]) -> str:
    """Create a Word document (or add to an existing .docx) from typed text, a spreadsheet column, PDF pages or a file.
    The copying happens here, not through the model, so long content arrives exactly as in the source. A PDF keeps its
    look (fonts, sizes, bold/italic, colours, alignment, hyperlinks) and a Word file is copied exactly."""
    paras, what, rich, why = _source_paragraphs(ctx, a)
    if sum(len(p) for p in paras) > MAX_WRITE:
        raise ToolError("that is too much text for one document; copy fewer pages")
    dst, note = _word_target(ctx, a["destination"], a.get("source"), a.get("name"))
    exists = os.path.exists(dst)
    if exists:
        need_read(ctx, dst)
    src = str(ctx.access.resolve(a["source"])) if a.get("source") else ""
    exact = bool(src) and readers.ext(src) == ".docx" and not exists and not a.get("text")
    if rich is not None and exists:
        rich, why = None, "formatting is only kept when the document is new, so the text was added without it"
    if rich is not None:
        extra = (f"Formatting kept: fonts, sizes, bold/italic, colours, alignment, spacing, {rich.links} link(s). "
                 f"Not copied: images ({rich.images}), boxes, backgrounds, real tables.")
    elif exact:
        extra = "Copied exactly, with all its formatting."
    else:
        extra = ("Plain text only" + (f" ({why})" if why else "") + "."
                 + ("" if a.get("plain") else " The formatting and links were NOT kept; say so to the user.")) if a.get("source") or why else ""
    detail = _preview("\n".join(paras)) + f"\n\n({len(paras)} paragraph(s) from {what})" + (f"\n{extra}" if extra else "")
    title = f"Add {what} to the end of {dst}?" if exists else (f"Copy {os.path.basename(src)} to {dst}?" if exact else f"Create {dst} with {what}?")
    if not ctx.ask(title, detail, risky=exists, where=dst):
        return "DENIED: the user said no. Nothing was changed."
    backup: str | None = None
    try:
        if exists:
            xml = docxio.load(dst)
            updated = docxio.append(xml, "\n".join(paras))
            backup = ctx.journal.backup(dst)
            docxio.save(dst, updated)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if exact:
                shutil.copyfile(src, dst)
            elif rich is not None:
                docxio.create_rich(dst, rich.paras, rich.page, rich.margins)
            else:
                docxio.create(dst, paras)
    except docxio.DocxError as e:
        raise ToolError(str(e)) from e
    _finish_write(ctx, dst, exists, backup)
    return f"OK: {'added' if exists else 'created'} {dst} with {what}{note}. {extra}".rstrip()


def _pdf_target(ctx: Ctx, raw: str | None, src: str, name: str | None) -> str:
    """Where the PDF goes: next to the source unless a folder or a name is given. Never an existing file."""
    folder_or_file = str(ctx.access.resolve(raw, must_exist=False)) if raw and raw.strip() else os.path.dirname(src)
    ctx.access.check_writable(folder_or_file)
    doc_exts = (".docx", ".doc", ".word", ".txt", ".md", ".rtf", ".odt", ".pdf")
    def stem(n: str) -> str:
        n = n.strip()
        return n[: -len(os.path.splitext(n)[1])] if os.path.splitext(n)[1].lower() in doc_exts else n
    if os.path.isdir(folder_or_file):
        base = stem(name) if name and name.strip() else stem(os.path.basename(src))
        if not base or re.search(r'[\\/:*?"<>|]', base):
            raise ToolError(f"'{name}' cannot be used as a file name (no \\ / : * ? \" < > |)")
        dst = os.path.join(folder_or_file, base + ".pdf")
    else:
        dst = os.path.join(os.path.dirname(folder_or_file), stem(os.path.basename(folder_or_file)) + ".pdf")
    ctx.access.check_writable(dst)
    if os.path.lexists(dst):
        raise ToolError(f"{dst} already exists; Hearth never overwrites. Choose another name")
    return dst


MAX_COPIES = 50


def _pdf_copy_targets(ctx: Ctx, raw: str | None, src: str, name: str | None, copies: int) -> tuple[str, list[str]]:
    """Several PDFs: the destination is always a folder (made if it is new). Names are '<name> 1.pdf', '<name> 2.pdf', ...;
    a number that is already taken is skipped, so nothing is ever overwritten."""
    folder = str(ctx.access.resolve(raw, must_exist=False)) if raw and raw.strip() else os.path.dirname(src)
    ctx.access.check_writable(folder)
    if os.path.exists(folder) and not os.path.isdir(folder):
        raise ToolError(f"{folder} is a file; give a folder for the {copies} copies")
    base = (name or "").strip()
    if os.path.splitext(base)[1].lower() in (".pdf", ".docx", ".doc", ".txt", ".md"):
        base = os.path.splitext(base)[0]
    base = base or os.path.splitext(os.path.basename(src))[0]
    if not base or re.search(r'[\\/:*?"<>|]', base):
        raise ToolError(f"'{name}' cannot be used as a file name (no \\ / : * ? \" < > |)")
    targets: list[str] = []
    n = 0
    while len(targets) < copies and n < copies + 1000:
        n += 1
        dst = os.path.join(folder, f"{base} {n}.pdf")
        ctx.access.check_writable(dst)
        if not os.path.lexists(dst):
            targets.append(dst)
    return folder, targets


def _save_pdf_copies(ctx: Ctx, src: str, a: dict[str, Any], copies: int) -> str:
    folder, targets = _pdf_copy_targets(ctx, a.get("destination"), src, a.get("name"), copies)
    try:
        text = "\n".join(docxio.paragraph_texts(docxio.load(src))) if readers.ext(src) == ".docx" else readers.read_text(src)
    except (docxio.DocxError, readers.UnreadableError) as e:
        raise ToolError(str(e)) from e
    new_folder = not os.path.isdir(folder)
    names = "\n".join(os.path.basename(t) for t in targets[:12]) + (f"\n... and {len(targets) - 12} more" if len(targets) > 12 else "")
    detail = (f"{'A new folder will be created. ' if new_folder else ''}Files:\n{names}\n\n" + _preview(text)
              + "\n\nMade with Microsoft Word or LibreOffice if either is installed, otherwise Hearth's own writer.")
    if not ctx.ask(f"Create {len(targets)} PDF copies of {os.path.basename(src)} in {folder}?", detail, risky=False, where=folder):
        return "DENIED: the user said no. Nothing was changed."
    items: list[dict[str, Any]] = []
    engine = ""
    try:
        if new_folder:
            os.makedirs(folder)
            items.append({"op": "create", "path": folder})
        for dst in targets:
            engine = pdfout.convert(src, dst)
            items.append({"op": "create", "path": dst})
    except (pdfout.PdfError, OSError) as e:
        if items:
            tools_more_batch(ctx, items)
        made = sum(i["path"].endswith(".pdf") for i in items)
        raise ToolError(f"{e}. {made} of {len(targets)} PDFs were made before this stopped") from e
    tools_more_batch(ctx, items)
    ctx.found = list(targets)
    return f"OK: created {len(targets)} PDFs in {folder} ({os.path.basename(targets[0])} to {os.path.basename(targets[-1])}), each a copy of {src}, made with {engine}"


def tools_more_batch(ctx: Ctx, items: list[dict[str, Any]]) -> None:
    from .tools_more import _batch
    _batch(ctx, items)


def save_as_pdf(ctx: Ctx, a: dict[str, Any]) -> str:
    """A PDF made from a Word or text file. The source name may come without its extension ('second run').
    copies=N makes N PDFs in one go, with one question."""
    src = read_path(ctx, a["source"], prefer=pdfout.CONVERTIBLE)
    if os.path.isdir(src):
        raise ToolError("the source is a folder, not a file")
    if readers.ext(src) not in pdfout.CONVERTIBLE:
        raise ToolError(f"{os.path.basename(src)} cannot be turned into a PDF; Word (.docx) and text files can")
    copies = int(a.get("copies") or 1)
    if copies < 1 or copies > MAX_COPIES:
        raise ToolError(f"copies must be between 1 and {MAX_COPIES}")
    if copies > 1:
        return _save_pdf_copies(ctx, src, a, copies)
    dst = _pdf_target(ctx, a.get("destination"), src, a.get("name"))
    try:
        text = "\n".join(docxio.paragraph_texts(docxio.load(src))) if readers.ext(src) == ".docx" else readers.read_text(src)
    except (docxio.DocxError, readers.UnreadableError) as e:
        raise ToolError(str(e)) from e
    detail = _preview(text) + "\n\nMade with Microsoft Word or LibreOffice if either is installed, otherwise Hearth's own writer."
    if not ctx.ask(f"Create the PDF {dst} from {os.path.basename(src)}?", detail, risky=False, where=dst):
        return "DENIED: the user said no. Nothing was changed."
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        engine = pdfout.convert(src, dst)
    except pdfout.PdfError as e:
        raise ToolError(str(e)) from e
    _finish_write(ctx, dst, False, None)
    return f"OK: created {dst} as a PDF of {src}, made with {engine}"


def make_folder(ctx: Ctx, a: dict[str, Any]) -> str:
    p = _writable(ctx, a["path"], must_exist=False, read=False)
    if os.path.exists(p):
        raise ToolError("that already exists")
    if not ctx.ask(f"Create folder {p}?", "", risky=False, where=p):
        return "DENIED: the user said no. Nothing was changed."
    os.makedirs(p)
    ctx.journal.record("create", path=p)
    return f"OK: created folder {p}"


def _destination(ctx: Ctx, src: str, raw_dst: str) -> str:
    dst = str(ctx.access.resolve(raw_dst, must_exist=False))
    ctx.access.check_writable(dst)
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src))
    if os.path.lexists(dst):
        raise ToolError(f"{dst} already exists; choose another name or folder")
    if dst == src or os.path.normcase(dst).startswith(os.path.normcase(src) + os.sep):
        raise ToolError("cannot move or copy something into itself")
    return dst


def _where(ctx: Ctx, src: str, dst: str) -> str:
    """The path that decides whether a harmless move/copy may skip the question: both ends must be trusted."""
    return dst if not ctx.access.read_free(dst) else src


def move_path(ctx: Ctx, a: dict[str, Any]) -> str:
    src = str(ctx.access.resolve(a["source"]))
    ctx.access.check_writable(src)
    if ctx.access.is_precious(src):
        raise ToolError("cannot move a drive, your home or standard folders, or a trusted folder itself")
    dst = _destination(ctx, src, a["destination"])
    if not ctx.ask(f"Move {src}", f"to {dst}", risky=False, where=_where(ctx, src, dst)):
        return "DENIED: the user said no. Nothing was changed."
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    ctx.journal.record("move", src=src, dst=dst)
    ctx.index.forget(src)
    ctx.index.sync()
    return f"OK: moved {src} to {dst}"


def copy_path(ctx: Ctx, a: dict[str, Any]) -> str:
    src = str(ctx.access.resolve(a["source"]))
    dst = _destination(ctx, src, a["destination"])
    if not ctx.ask(f"Copy {src}", f"to {dst}", risky=False, where=_where(ctx, src, dst)):
        return "DENIED: the user said no. Nothing was changed."
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)
    ctx.journal.record("copy", path=dst)
    ctx.index.sync()
    return f"OK: copied {src} to {dst}"


def delete_path(ctx: Ctx, a: dict[str, Any]) -> str:
    p = str(ctx.access.resolve(a["path"]))
    ctx.access.check_writable(p)
    if ctx.access.is_precious(p):
        raise ToolError("cannot delete a drive, your home or standard folders, or a trusted folder itself")
    detail = "It moves to Hearth's trash and can be restored with undo."
    if os.path.isdir(p):
        count = sum(len(f) for _, _, f in os.walk(p))
        detail = f"This folder holds {count} file(s). " + detail
    if not ctx.ask(f"Delete {p}?", detail, risky=True):
        return "DENIED: the user said no. Nothing was changed."
    slot = ctx.journal.trash_slot()
    trashed = str(slot / os.path.basename(p))
    shutil.move(p, trashed)
    ctx.journal.record("delete", path=p, trashed=trashed)
    ctx.index.forget(p)
    return f"OK: deleted {p} (it is in Hearth's trash and can be restored with undo)"


def open_file(ctx: Ctx, a: dict[str, Any]) -> str:
    p = str(ctx.access.resolve(a["path"]))
    if not os.path.isdir(p) and readers.ext(p) not in OPENABLE:
        raise ToolError(f"I only open documents, images, media and folders, not {readers.ext(p) or 'this kind of'} files")
    if not ctx.ask(f"Open {p} with your default app?", "", risky=True):
        return "DENIED: the user said no. Nothing was changed."
    try:
        ctx.opener(p)
    except OSError as e:
        raise ToolError(f"could not open it: {e}") from e
    return f"OK: opened {p}"


# ---------------------------------------------------------------- finding documents and working with spreadsheets

def _target(ctx: Ctx, a: dict[str, Any]) -> str:
    raw = a.get("path") or ctx.selected or ctx.focus.get("path")
    if not raw:
        raise ToolError("which file? Give a path, or ask the user to select one")
    p = read_path(ctx, raw)
    if os.path.isdir(p) or readers.ext(p) not in sheets.SHEET_EXTS:
        raise ToolError(f"{os.path.basename(p)} is not a spreadsheet (.xlsx, .csv or .tsv)")
    return p


def _books(p: str) -> list[sheets.Sheet]:
    try:
        return sheets.load(p)
    except sheets.SheetError as e:
        raise ToolError(str(e)) from e


def _pick(ctx: Ctx, p: str, books: list[sheets.Sheet], name: str | None) -> sheets.Sheet:
    want = name or (ctx.focus.get("sheet") if ctx.focus.get("path") == p else None)
    if want:
        for b in books:
            if b.name.lower() == str(want).lower():
                return b
        raise ToolError(f"no sheet called '{want}'. Sheets: {', '.join(b.name for b in books)}")
    return books[0]


def _row_text(sh: sheets.Sheet, row: int) -> str:
    heads = sh.headers()
    cells = sh.cells.get(row, {})
    return " | ".join(f"{heads.get(c) or sheets.col_letters(c)}={cells[c]}" for c in sorted(cells) if cells[c] != "")


def sheet_info(ctx: Ctx, a: dict[str, Any]) -> str:
    p = _target(ctx, a)
    out = [p]
    for sh in _books(p):
        heads = ", ".join(f"{h} ({sheets.col_letters(c)})" for c, h in sh.headers().items()) or "(empty)"
        out.append(f"sheet '{sh.name}': {max(len(sh.rows) - 1, 0)} data row(s)\n  columns: {heads}")
        for r in sh.rows[1:4]:
            out.append(f"  row {r}: {_row_text(sh, r)}")
    return "\n".join(out)


def sheet_find(ctx: Ctx, a: dict[str, Any]) -> str:
    p = _target(ctx, a)
    books = _books(p)
    text = a["text"].strip().lower()
    wanted = [_pick(ctx, p, books, a.get("sheet"))] if a.get("sheet") else books
    lines: list[str] = []
    total = 0
    first: tuple[sheets.Sheet, list[int]] | None = None
    for sh in wanted:
        try:
            col = sheets.resolve_column(sh, a["column"]) if a.get("column") else None
        except sheets.SheetError as e:
            if len(wanted) == 1:
                raise ToolError(str(e)) from e
            continue
        rows = [r for r in sh.rows[1:] if any(text in v.lower() for c, v in sh.cells[r].items() if col in (None, c))]
        if rows and first is None:
            first = (sh, rows)
        total += len(rows)
        for r in rows[:20]:
            lines.append(f"[{sh.name}] row {r}: {_row_text(sh, r)}")
    if first is None:
        heads = {h.strip().lower() for sh in wanted for h in sh.headers().values()}
        hint = ""
        if any(text == h or (len(text) >= 3 and text in h) for h in heads):
            hint = (f" Note: '{a['text']}' is (part of) a column header, not a value. To list the values under a column "
                    "use sheet_column.")
        return f"No row contains '{a['text']}'" + (f" in column '{a['column']}'." if a.get("column") else ".") + hint
    ctx.focus = {"path": p, "sheet": first[0].name, "rows": first[1][:200]}
    more = f"\n... {total - 20} more row(s); narrow it with the column or a longer text" if total > 20 else ""
    return f"{total} matching row(s) in {os.path.basename(p)}:\n" + "\n".join(lines) + more


def sheet_column(ctx: Ctx, a: dict[str, Any]) -> str:
    """The values under one column. The header may be a title row's neighbour and may be only part of the real name
    ('Company' finds 'Company Name')."""
    p = _target(ctx, a)
    books = _books(p)
    wanted = [_pick(ctx, p, books, a.get("sheet"))] if a.get("sheet") else books
    limit = max(1, min(int(a.get("limit") or 100), 500))
    out: list[str] = []
    err: sheets.SheetError | None = None
    for sh in wanted:
        try:
            hrow, col = _find_column(sh, str(a["column"]))
        except sheets.SheetError as e:
            err = err or e
            continue
        head = sh.headers(hrow).get(col, sheets.col_letters(col))
        data = [(r, sh.get(r, col)) for r in sh.rows if r > hrow and any(v.strip() for v in sh.cells.get(r, {}).values())]
        filled = [(r, v) for r, v in data if v.strip()]
        lines: list[str]
        if a.get("unique"):
            counts: dict[str, int] = {}
            for _, v in filled:
                counts[v] = counts.get(v, 0) + 1
            lines = [f"{v}  (x{n})" if n > 1 else v for v, n in counts.items()]
            what = f"{len(counts)} different value(s) in {len(filled)} filled cell(s)"
        else:
            lines = [f"row {r}: {v}" for r, v in filled]
            what = f"{len(filled)} value(s)"
        empty = len(data) - len(filled)
        out.append(f"Column '{head}' ({sheets.col_letters(col)}) of sheet '{sh.name}' in {os.path.basename(p)}: {what}"
                   + (f", {empty} empty" if empty else "") + ":")
        out += lines[:limit]
        if len(lines) > limit:
            out.append(f"... {len(lines) - limit} more; pass limit=<bigger number> (max 500) or unique=true")
    if not out:
        raise ToolError(str(err) if err else "the sheet is empty")
    return "\n".join(out)


def sheet_set_cell(ctx: Ctx, a: dict[str, Any]) -> str:
    p = _target(ctx, a)
    ctx.access.check_writable(p)
    books = _books(p)
    sh = _pick(ctx, p, books, a.get("sheet"))
    row = a.get("row")
    if row is None:
        rows = ctx.focus.get("rows", []) if ctx.focus.get("path") == p and ctx.focus.get("sheet") == sh.name else []
        if len(rows) != 1:
            raise ToolError("which row? Use sheet_find first to locate it, then pass row=<number>" if not rows else
                            f"{len(rows)} rows are in focus ({', '.join(map(str, rows[:8]))}); pass row=<number>")
        row = rows[0]
    if row not in sh.cells:
        raise ToolError(f"row {row} is empty or does not exist")
    try:
        col = sheets.resolve_column(sh, a["column"])
    except sheets.SheetError as e:
        raise ToolError(str(e)) from e
    old = sh.get(row, col)
    expect = a.get("expect")
    if expect is not None and old.strip() != str(expect).strip():
        raise ToolError(f"{sheets.col_letters(col)}{row} holds '{old}', not '{expect}'. Nothing was changed")
    value = a["value"]
    head = sh.headers().get(col, sheets.col_letters(col))
    detail = f"{_row_text(sh, row)}\n\n{head}: {old or '(empty)'}  ->  {value or '(empty)'}"
    if not ctx.ask(f"Change {sheets.col_letters(col)}{row} ({head}) in {os.path.basename(p)}?", detail, risky=True):
        return "DENIED: the user said no. Nothing was changed."
    backup = ctx.journal.backup(p)
    try:
        sheets.set_cell(p, sh, row, col, value)
    except sheets.SheetError as e:
        raise ToolError(str(e)) from e
    _finish_write(ctx, p, True, backup)
    ctx.focus = {"path": p, "sheet": sh.name, "rows": [row]}
    return f"OK: {head} in row {row} changed from '{old}' to '{value}' in {p}"


def _P(name: str, kind: type, doc: str, required: bool = True) -> Param:
    return Param(name, kind, required, doc)


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("list_folder", "list the names and sizes in a folder anywhere on the computer (no path = the chosen folder, or the "
         "usual places); never opens file contents",
         [_P("path", str, "folder", False)], False, list_folder),
    Tool("folder_summary", "count the files in a folder and everything under it BY TYPE, then the total "
         "(no path = the chosen folder)",
         [_P("path", str, "folder", False)], False, folder_summary),
    Tool("find_files", "find files by NAME and/or TYPE anywhere (names, sizes, dates only; no content is read). "
         "kind lists every file of a type, newest first",
         [_P("pattern", str, "name or pattern, * is a wildcard", False),
          _P("kind", str, "excel, word, pdf, presentation, text, code, image, audio, video, archive", False),
          _P("folder", str, "where to look: a path, a drive like E:, or Downloads/Desktop/Documents (default: the chosen folder, else home)", False),
          _P("limit", int, "how many to show, newest first (default 40); use 5 for 'the 5 most recent'", False)], False, find_files),
    Tool("search_text", "search INSIDE files for words (also spreadsheet column names); the user is asked first for "
         "folders that are not already open",
         [_P("query", str, "words to look for"), _P("kind", str, "excel, word, pdf, text, ...", False),
          _P("folder", str, "folder to search (asks permission if needed)", False)], False, search_text),
    Tool("sheet_info", "show a spreadsheet's sheets, column headers and first rows (path defaults to the selected file)",
         [_P("path", str, "spreadsheet", False)], False, sheet_info),
    Tool("sheet_column", "list the VALUES under one column (e.g. 'what are the values under Company'); the column may be "
         "named loosely; unique=true gives each different value once",
         [_P("column", str, "column header or letter"), _P("unique", bool, "each different value once, with counts", False),
          _P("limit", int, "how many to show (default 100)", False), _P("sheet", str, "sheet name", False),
          _P("path", str, "spreadsheet", False)], False, sheet_column),
    Tool("sheet_find", "find rows whose cells contain some TEXT (a value, never a column name), optionally only in one "
         "column (e.g. 'Jack' in 'Name'); remembers them",
         [_P("text", str, "text to look for"), _P("column", str, "column header or letter", False),
          _P("sheet", str, "sheet name", False), _P("path", str, "spreadsheet", False)], False, sheet_find),
    Tool("read_file", "read the text of a text, .docx, .xlsx or .pdf file (the user is asked first if not allowed yet; long files come in parts: use offset)",
         [_P("path", str, "file"), _P("offset", int, "start character", False)], False, read_file),
    Tool("file_info", "size and modified date of a file or folder", [_P("path", str, "file or folder")], False, file_info),
    Tool("write_file", "create a new text file (overwrite=true replaces an existing one)",
         [_P("path", str, "file"), _P("content", str, "full text"), _P("overwrite", bool, "replace existing", False)],
         True, write_file),
    Tool("replace_text", "change text inside a text or .docx file; 'old' must match exactly once",
         [_P("path", str, "file"), _P("old", str, "exact text to replace"), _P("new", str, "replacement"),
          _P("replace_all", bool, "replace every occurrence", False)], True, replace_text),
    Tool("append_text", "add text to the end of a file (creates it if missing)",
         [_P("path", str, "file"), _P("text", str, "text to add")], True, append_text),
    Tool("sheet_set_cell", "change ONE cell of a spreadsheet; row defaults to the single row from the last sheet_find; "
         "expect = the value you believe is there now (safety check)",
         [_P("column", str, "column header or letter"), _P("value", str, "new value"), _P("row", int, "row number", False),
          _P("expect", str, "current value", False), _P("sheet", str, "sheet name", False),
          _P("path", str, "spreadsheet", False)], True, sheet_set_cell),
    Tool("copy_to_word", "create a Word (.docx) file, or add to one, from a spreadsheet column, PDF pages, another file or "
         "typed text (Word output only, never PDF); a PDF or Word source keeps its formatting and links (never retype "
         "long content yourself). destination may be a folder or drive: then pass name",
         [_P("destination", str, "the .docx file, or a folder such as Downloads"),
          _P("source", str, "file to copy from (.pdf, .xlsx, .csv, .txt, .docx)", False),
          _P("pages", str, "PDF pages, e.g. 1-23 or 2,5", False), _P("column", str, "spreadsheet column header or letter", False),
          _P("sheet", str, "sheet name", False), _P("text", str, "text to put in the document", False),
          _P("name", str, "file name for a NEW document when destination is a folder or drive, e.g. second run", False),
          _P("plain", bool, "true = text only, without formatting", False)], True, copy_to_word),
    Tool("save_as_pdf", "make one PDF, or copies=N PDFs at once, from a Word (.docx) or text file; the source may be named without its extension; "
         "destination is a folder or drive (optional), name the new file (optional)",
         [_P("source", str, "the Word or text file, as the user named it"), _P("destination", str, "folder or drive for the PDF", False),
          _P("name", str, "file name for the PDF, e.g. third check", False),
          _P("copies", int, "how many PDF copies to make in ONE call (names get ' 1', ' 2', ...); destination is then a folder, made if new", False)], True, save_as_pdf),
    Tool("make_folder", "create a folder", [_P("path", str, "folder")], True, make_folder),
    Tool("move_path", "move or rename a file or folder (destination may be an existing folder)",
         [_P("source", str, "from"), _P("destination", str, "to")], True, move_path),
    Tool("copy_path", "copy a file or folder", [_P("source", str, "from"), _P("destination", str, "to")], True, copy_path),
    Tool("delete_path", "delete a file or folder (goes to a recoverable trash)", [_P("path", str, "what to delete")],
         True, delete_path),
    Tool("open_file", "open a document/image/folder in the user's default app", [_P("path", str, "what to open")],
         True, open_file),
]}


def run_tool(ctx: Ctx, name: str, args: Any) -> str:
    """Execute one tool call and always return text the model can read (errors included)."""
    tool = TOOLS.get(name)
    if tool is None:
        return f"ERROR: there is no tool called '{name}'. Available: {', '.join(TOOLS)}"
    if tool.changes and not ctx.allow_changes:
        return ("ERROR: not done. The user asked a question or to look at something, not to change anything, so no "
                "change is allowed this turn. Do not try again: answer with what you found. If the user does want a "
                "change they will ask for it.")
    try:
        out = tool.fn(ctx, tool.clean(args))
    except DeniedError as e:
        return f"DENIED: the user did not allow this ({e}). Do not ask again; tell the user nothing was opened or changed."
    except (ToolError, AccessError, readers.UnreadableError, sheets.SheetError, docxio.DocxError) as e:
        return f"ERROR: {e}"
    except OSError as e:
        return f"ERROR: the operating system refused: {e.strerror or e}"
    return out if len(out) <= MAX_OBS else out[:MAX_OBS] + "\n[output cut]"


from . import tools_more  # noqa: E402,F401  registers the extra tools in TOOLS
