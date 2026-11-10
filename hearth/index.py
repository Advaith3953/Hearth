"""Full-text index of the folders Hearth may read (SQLite FTS5). Local file, no embeddings, no network."""
from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .access import Access, within
from .readers import MAX_READ, UnreadableError, indexable, read_text

SKIP_DIRS = {"node_modules", "__pycache__", "venv", "site-packages", "appdata", "$recycle.bin"}
MAX_FILES = 20_000   # per sync; a huge folder is indexed over several syncs
CHUNK = 1200


@dataclass
class Hit:
    path: str
    snippet: str


def chunks(text: str, size: int = CHUNK) -> list[str]:
    out: list[str] = []
    cur = ""
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:  # a very long paragraph: cut on whitespace
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:cut])
            para = para[cut:].lstrip()
        if len(cur) + len(para) + 2 > size and cur:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


class Index:
    def __init__(self, db: Path, access: Access) -> None:
        self.db = db
        self.access = access
        self.complete = True   # False when the last sync stopped early (time or file limit)

    def _conn(self) -> sqlite3.Connection:
        self.db.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER)")
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, n UNINDEXED, text, "
                  "tokenize='porter unicode61')")
        return c

    @staticmethod
    def _put(c: sqlite3.Connection, path: str, st: os.stat_result) -> None:
        c.execute("DELETE FROM chunks WHERE path=?", (path,))
        try:
            text = read_text(path)
        except UnreadableError:
            text = ""
        for i, part in enumerate(chunks(text)):
            c.execute("INSERT INTO chunks(path, n, text) VALUES (?,?,?)", (path, i, part))
        c.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (path, st.st_mtime, st.st_size))

    def walk(self, roots: list[str] | None = None, deadline: float | None = None) -> list[str]:
        found: list[str] = []
        a = self.access
        for root in roots if roots is not None else a.readable_roots():
            for dp, dns, fns in os.walk(root):
                dns[:] = [d for d in dns if not d.startswith(".") and d.lower() not in SKIP_DIRS
                          and not a.is_blocked(os.path.join(dp, d)) and not os.path.islink(os.path.join(dp, d))]
                for fn in fns:
                    p = os.path.join(dp, fn)
                    if not fn.startswith(".") and indexable(p) and not os.path.islink(p) and not a.is_blocked(p):
                        found.append(p)
                if len(found) >= MAX_FILES or (deadline is not None and time.monotonic() > deadline):
                    self.complete = False
                    return found
        return found

    def sync(self, roots: list[str] | None = None, seconds: float = 25.0) -> tuple[int, int]:
        """Bring the index up to date for the readable folders (or just `roots`). Returns (indexed or refreshed, dropped)."""
        c = self._conn()
        try:
            self.complete = True
            scope = roots if roots is not None else self.access.readable_roots()
            known = {p: (m, s) for p, m, s in c.execute("SELECT path, mtime, size FROM files")}
            present = set()
            changed = 0
            end = time.monotonic() + seconds
            for p in self.walk(scope, end):
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_size > MAX_READ:
                    continue
                present.add(p)
                if known.get(p) != (st.st_mtime, st.st_size):
                    self._put(c, p, st)
                    changed += 1
                    if time.monotonic() > end:
                        self.complete = False
                        break
            gone = [p for p in known if p not in present and (self.complete and any(within(p, r) for r in scope)
                                                              or self.access.is_blocked(p))]
            for p in gone:
                c.execute("DELETE FROM chunks WHERE path=?", (p,))
                c.execute("DELETE FROM files WHERE path=?", (p,))
            c.commit()
            return changed, len(gone)
        finally:
            c.close()

    def refresh(self, path: str) -> None:
        """Re-index one file after Hearth changed it (or drop it if it is gone)."""
        real = os.path.realpath(path)
        c = self._conn()
        try:
            if os.path.isfile(real) and indexable(real) and self.access.read_free(real):
                self._put(c, real, os.stat(real))
            else:
                self._drop(c, real)
            c.commit()
        finally:
            c.close()

    def forget(self, path: str) -> None:
        c = self._conn()
        try:
            self._drop(c, os.path.realpath(path))
            c.commit()
        finally:
            c.close()

    @staticmethod
    def _drop(c: sqlite3.Connection, real: str) -> None:
        like = real.rstrip("\\/") + os.sep
        like = like.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        for table in ("chunks", "files"):
            c.execute(f"DELETE FROM {table} WHERE path=? OR path LIKE ? ESCAPE '\\'", (real, like))

    def search(self, query: str, limit: int = 6, folder: str = "") -> list[Hit]:
        """Files with ALL the words in one passage come first and, when there are any, are all that is returned;
        only if none has them all does it fall back to files with ANY of the words."""
        words = re.findall(r"\w+", query, re.UNICODE)[:12]
        if not words:
            return []
        quoted = ['"' + w.replace('"', "") + '"' for w in words]
        c = self._conn()
        try:
            rows: list[tuple[str, str]] = []
            for joiner in (" AND ", " OR "):
                rows = c.execute("SELECT path, snippet(chunks, 2, '[', ']', ' … ', 28) FROM chunks WHERE chunks MATCH ? "
                                 "ORDER BY rank LIMIT ?", (joiner.join(quoted), limit * (40 if folder else 4))).fetchall()
                if rows or len(quoted) == 1:
                    break
        finally:
            c.close()
        hits: list[Hit] = []
        seen: set[str] = set()
        for path, snip in rows:
            if path in seen or not self.access.read_free(path) or (folder and not within(path, folder)):
                continue
            seen.add(path)
            hits.append(Hit(path, " ".join(snip.split())))
            if len(hits) >= limit:
                break
        return hits
