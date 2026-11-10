"""The permission boundary. Every path the assistant touches goes through Sandbox.resolve().

Rules: a path must resolve (after ``..`` and symbolic links) to somewhere inside a folder the user shared; Hearth's own
data directory is never reachable even when it lies inside a shared folder; links and shortcuts are not followed.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path


class SandboxError(Exception):
    """A request that is outside what the user has shared."""


def _within(child: str, parent: str) -> bool:
    c, p = os.path.normcase(child), os.path.normcase(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives
        return False


class Sandbox:
    def __init__(self, roots: Iterable[str | Path], protected: Iterable[str | Path] = ()) -> None:
        self.roots = [os.path.realpath(r) for r in roots]
        self.protected = [os.path.realpath(p) for p in protected]

    def contains(self, real: str) -> bool:
        return any(_within(real, r) for r in self.roots) and not any(_within(real, p) for p in self.protected)

    def is_root(self, real: str) -> bool:
        return os.path.normcase(real) in {os.path.normcase(r) for r in self.roots}

    def resolve(self, raw: str, *, must_exist: bool = True) -> Path:
        """Turn what the model (or user) wrote into a real absolute path, or raise SandboxError."""
        if not self.roots:
            raise SandboxError("no folders have been shared yet (run: hearth grant <folder>)")
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            raise SandboxError("empty or invalid path")
        text = os.path.expanduser(raw.strip())
        # relative paths: the first shared folder that already has it, else the first shared folder
        candidates = [text] if os.path.isabs(text) else [os.path.join(r, text) for r in self.roots]
        chosen = ""
        for c in candidates:
            c = os.path.abspath(c)
            if os.path.lexists(c):
                chosen = c
                break
        if not chosen:
            chosen = os.path.abspath(candidates[0])
        real = os.path.realpath(chosen)
        if not self.contains(real):
            raise SandboxError(f"'{raw}' is outside the folders you shared")
        if os.path.islink(chosen):
            raise SandboxError(f"'{raw}' is a link/shortcut; those are not followed")
        if must_exist and not os.path.exists(real):
            raise SandboxError(f"'{raw}' does not exist")
        return Path(real)
