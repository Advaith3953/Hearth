"""Shared building blocks for the tools: context, tool definition, argument checking, folder walking."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from .access import Access, dedupe
from .index import Index
from .journal import Journal


class ToolError(Exception):
    """A problem the model can read and react to (bad path, text not found, ...)."""


class DeniedError(Exception):
    """The user said no to a permission question."""


ALLOW_FILE = "Allow this file"
ALLOW_FOLDER = "Allow this folder until Hearth closes"
DENY = "Don't allow"
ALLOW_ONE = "Allow"
ALLOW_BATCH = "Allow all new files in this request"


def default_open(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@dataclass
class Ctx:
    access: Access
    index: Index
    journal: Journal
    confirm: Callable[[str, str], bool]
    opener: Callable[[str], None] = default_open
    auto_approve_safe: bool = False  # skip the question for harmless, undoable additions (new files/folders, copies, moves)
    selected: str | None = None      # the file the user picked in the web page; sheet tools default to it
    focus: dict[str, Any] = field(default_factory=dict)  # last spreadsheet rows shown: {path, sheet, rows}
    found: list[str] = field(default_factory=list)       # files the find tools turned up most recently
    found_why: dict[str, str] = field(default_factory=dict)  # path -> why it matched (for the file cards)
    choose: Callable[[str, str, list[str]], int] | None = None  # multi-choice permission question -> index of the answer
    batch_ok: bool = False           # the user said "allow all" for new files/copies during this request; reset every request
    allow_changes: bool = True       # the agent turns this off for a question, so no change tool can run at all

    def ask(self, title: str, detail: str, risky: bool, where: str | None = None) -> bool:
        """Ask before a change. Harmless additions may skip the question, but only inside folders the user trusts."""
        if self.auto_approve_safe and not risky and (where is None or self.access.read_free(where)):
            return True
        if not risky and self.batch_ok:
            return True                       # the user already approved this kind of addition for the whole request
        if not risky and self.choose is not None:
            n = self.choose(title, detail, [ALLOW_ONE, ALLOW_BATCH, DENY])
            if n == 1:
                self.batch_ok = True
            return n in (0, 1)
        return self.confirm(title, detail)

    def pick(self, title: str, detail: str, options: list[str]) -> int:
        if self.choose is not None:
            return self.choose(title, detail, options)
        return 0 if self.confirm(title, detail) else len(options) - 1


def need_read(ctx: Ctx, real: str) -> None:
    """Make sure Hearth may read this file or folder: free inside trusted/allowed places, otherwise ask the user."""
    a = ctx.access
    if a.read_free(real):
        return
    isdir = os.path.isdir(real)
    options = [ALLOW_FOLDER, DENY] if isdir else [ALLOW_FILE, ALLOW_FOLDER, DENY]
    folder = real if isdir else os.path.dirname(real)
    what = os.path.basename(real.rstrip("\\/")) or real
    note = "\nThis is an operating-system folder; it will only ever be read, never changed." if a.is_system(real) else ""
    detail = (f"{real}\nHearth would read the text inside to answer your request. Nothing is changed or sent anywhere."
              f"{note}\n'{ALLOW_FOLDER}' covers: {folder}")
    choice = options[ctx.pick(f"Allow Hearth to read {what}?", detail, options)]
    if choice == ALLOW_FILE:
        a.grant_file(real)
    elif choice == ALLOW_FOLDER:
        a.grant_dir(folder)
    else:
        raise DeniedError(real)


def read_path(ctx: Ctx, raw: str, prefer: tuple[str, ...] | None = None) -> str:
    """Resolve a path the model gave and make sure its content may be read."""
    real = str(ctx.access.resolve(raw, prefer=prefer))
    need_read(ctx, real)
    return real


PATH_PARAMS = {"path", "source", "destination", "folder"}
_CONTROL_AS_ESCAPE = {"\a": "\\a", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\v": "\\v", "\f": "\\f", "\r": "\\r"}


def repair_path(v: str) -> str:
    """A model that writes "D:\\folder1\\new.docx" in JSON without doubling the backslashes produces a form feed and a
    newline inside the path. No real path contains control characters, so turn them back into the two characters."""
    return "".join(_CONTROL_AS_ESCAPE.get(c, c) for c in v)


@dataclass
class Param:
    name: str
    kind: type
    required: bool
    doc: str


@dataclass
class Tool:
    name: str
    doc: str
    params: list[Param]
    changes: bool
    fn: Callable[[Ctx, dict[str, Any]], str] = field(repr=False)

    def signature(self) -> str:
        args = ", ".join(f"{p.name}{'' if p.required else '?'}: {p.kind.__name__}" for p in self.params)
        return f"{self.name}({args}) - {self.doc}"

    def clean(self, args: Any) -> dict[str, Any]:
        """Validate and gently coerce what a small model produced (\"3\" -> 3, \"true\" -> True)."""
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ToolError("args must be a JSON object")
        known = {p.name: p for p in self.params}
        extra = [k for k in args if k not in known]
        if extra:
            raise ToolError(f"unknown argument(s) {extra}; {self.name} takes: {', '.join(known) or 'nothing'}")
        out: dict[str, Any] = {}
        for p in self.params:
            if p.name not in args:
                if p.required:
                    raise ToolError(f"missing required argument '{p.name}'")
                continue
            v = args[p.name]
            if p.kind is int and isinstance(v, str) and v.strip().lstrip("-").isdigit():
                v = int(v)
            if p.kind is bool and isinstance(v, str) and v.lower() in ("true", "false"):
                v = v.lower() == "true"
            if p.kind is str and isinstance(v, (int, float)) and not isinstance(v, bool):
                v = str(v)
            if not isinstance(v, p.kind) or (p.kind is int and isinstance(v, bool)):
                raise ToolError(f"argument '{p.name}' must be {p.kind.__name__}")
            out[p.name] = repair_path(v) if p.name in PATH_PARAMS and isinstance(v, str) else v
        return out




NOISY = {"appdata", "$recycle.bin", "system volume information", "windows.old", "$windows.~bt", "node_modules",
         "__pycache__", "venv", "site-packages", "lost+found"}


def is_link(path: str) -> bool:
    """Symlinks, and on Windows junctions/reparse points, which must not be followed (loops, other drives)."""
    if os.path.islink(path):
        return True
    if sys.platform == "win32":
        try:
            return bool(os.lstat(path).st_file_attributes & 0x400)  # type: ignore[attr-defined]
        except OSError:
            return True
    return False


class Budget:
    """Keeps a scan of a big disk from running forever."""

    def __init__(self, seconds: float = 8.0, files: int = 300_000) -> None:
        self.end = time.monotonic() + seconds
        self.left = files
        self.truncated = False

    def spent(self, n: int = 1) -> bool:
        self.left -= n
        if self.left < 0 or time.monotonic() > self.end:
            self.truncated = True
        return self.truncated


def default_scope(ctx: Ctx) -> list[str]:
    """Where 'here' is when the user gave no place: the trusted folders, else their home folder."""
    return dedupe(ctx.access.trusted) or [ctx.access.home]


def walk(ctx: Ctx, start: str, budget: Budget | None = None) -> Iterator[tuple[str, list[str], list[str]]]:
    """Walk a folder tree. Skips hidden folders, noisy/system folders (unless the walk starts inside one), secret
    stores and links, so a scan of a whole disk stays fast and safe."""
    a = ctx.access
    inside_system = a.is_system(start)
    for dp, dns, fns in os.walk(start):
        if budget is not None and budget.spent(len(fns) + 1):
            return
        keep: list[str] = []
        for d in sorted(dns):
            full = os.path.join(dp, d)
            if d.startswith(".") or d.lower() in NOISY or a.is_blocked(full) or is_link(full):
                continue
            if a.is_system(full) and not inside_system:
                continue
            keep.append(d)
        dns[:] = keep
        yield dp, dns, sorted(f for f in fns if not a.is_blocked(os.path.join(dp, f)))
