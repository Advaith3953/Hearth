"""Finding files by what is in them (content, column headers, names) inside the places Hearth may read."""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass

from . import sheets
from .access import within
from .filetypes import KINDS, ext, kind_of, normalize_kind
from .tools_base import Budget, Ctx, walk

__all__ = ["Found", "KINDS", "kind_of", "normalize_kind", "scan_headers", "search"]


@dataclass
class Found:
    path: str
    why: str = ""


def scan_headers(ctx: Ctx, word: str, roots: list[str], budget: Budget, limit: int = 30) -> list[Found]:
    """Spreadsheets whose column headers contain `word` (case-insensitive), e.g. 'Country'."""
    want = word.strip().lower()
    out: list[Found] = []
    if not want:
        return out
    for root in roots:
        for dp, _, fns in walk(ctx, root, budget):
            for fn in fns:
                p = os.path.join(dp, fn)
                if ext(fn) not in sheets.SHEET_EXTS or fn.startswith("~$") or not ctx.access.read_free(p):
                    continue
                try:
                    books = sheets.load(p, headers_only=True)
                except (sheets.SheetError, OSError):
                    continue
                for sh in books:
                    hits = [h for h in sh.headers().values() if want in h.lower()]
                    if hits:
                        out.append(Found(p, f"sheet '{sh.name}' has column(s): {', '.join(hits[:4])}"))
                        break
                if len(out) >= limit:
                    return out
    return out


def search(ctx: Ctx, query: str, kind: str = "", roots: list[str] | None = None, limit: int = 12) -> tuple[list[Found], bool]:
    """Content, column-header and file-name matches among files Hearth may read, optionally of one kind.
    Returns (matches, scan_was_cut_short)."""
    exts = KINDS.get(kind)
    scope = roots if roots is not None else ctx.access.readable_roots()
    results: dict[str, Found] = {}
    budget = Budget(8.0)
    if kind in ("excel", ""):
        for f in scan_headers(ctx, query.strip(), scope, budget):
            results.setdefault(f.path, f)
    folder_of = [os.path.normcase(r) for r in scope]
    for h in ctx.index.search(query, limit=limit * 3):
        if (exts is None or ext(h.path) in exts) and any(within(h.path, r) for r in folder_of):
            results.setdefault(h.path, Found(h.path, h.snippet))
    toks = [t.lower() for t in query.split() if t.strip()]
    if toks:
        for root in scope:
            for dp, _dns, fns in walk(ctx, root, budget):
                for n in fns:
                    p = os.path.join(dp, n)
                    if (exts is None or ext(p) in exts) and all(fnmatch.fnmatch(n.lower(), f"*{t}*") for t in toks):
                        results.setdefault(p, Found(p, "file name matches"))
    return list(results.values())[:limit], budget.truncated
