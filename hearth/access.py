"""Who may do what, anywhere on this computer. Every path the assistant touches goes through Access.resolve().

The model:
  * LISTING is free everywhere: names, sizes, types and dates of files and folders (never their content).
  * READING a file's content needs the user's permission, except in *trusted folders* the user chose with `hearth grant`
    and in folders/files they allowed during this session.
  * CHANGING anything (create, edit, move, copy, delete, open) always needs the user's approval, one change at a time.
  * Some places are never opened at all: Hearth's own data, password/key stores and browser profiles. Operating-system
    folders can be read with permission but never changed.
Links and shortcuts are not followed, and ``..`` is resolved before any decision is made.
"""
from __future__ import annotations

import os
import re
import string
import sys
from collections.abc import Iterable
from pathlib import Path


class AccessError(Exception):
    """A request Hearth will not carry out (blocked place, bad path, ...)."""


def _norm(p: str) -> str:
    return os.path.normcase(p)


def within(child: str, parent: str) -> bool:
    c, p = _norm(child), _norm(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives
        return False


def dedupe(paths: Iterable[str]) -> list[str]:
    """Drop folders that are inside another folder of the list (and exact duplicates)."""
    items = list(dict.fromkeys(paths))
    return [p for p in items if not any(q != p and within(p, q) for q in items)]


# Places that hold secrets. Never listed, read or changed, even if the user says yes: a prompt-injected or mistaken
# approval must not be able to hand over a password store. Best effort, not a complete list (see SECURITY.md).
_BLOCK_PARTS = (
    ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".password-store", ".config/gcloud", "keychains",
    "microsoft/credentials", "microsoft/protect", "microsoft/vault", "windows/system32/config",
    "google/chrome/user data", "microsoft/edge/user data", "mozilla/firefox/profiles", "bravesoftware",
    "application support/google/chrome", "application support/firefox", ".mozilla/firefox", ".config/google-chrome",
    ".config/chromium", ".config/bravesoftware",
)
_BLOCK_NAMES = {".netrc", ".git-credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "shadow", "sudoers"}
_BLOCK_EXTS = {".kdbx", ".kdb", ".pfx", ".p12", ".ppk"}


def _system_dirs() -> list[str]:
    out: list[str] = []
    if sys.platform == "win32":
        for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "ProgramW6432"):
            if os.environ.get(var):
                out.append(os.path.realpath(os.environ[var]))
    else:
        out += [os.path.realpath(p) for p in ("/bin", "/sbin", "/usr", "/lib", "/lib64", "/etc", "/boot", "/dev", "/proc",
                                              "/sys", "/System", "/Library", "/Applications") if os.path.exists(p)]
    return out


def drives() -> list[str]:
    if sys.platform == "win32":
        return [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
    return ["/"]


class Access:
    def __init__(self, trusted: Iterable[str | Path] = (), protected: Iterable[str | Path] = ()) -> None:
        self.trusted = [os.path.realpath(r) for r in trusted]
        self.protected = [os.path.realpath(p) for p in protected]
        self.session_dirs: list[str] = []      # folders the user allowed until Hearth closes
        self.session_files: set[str] = set()   # single files the user allowed until Hearth closes
        self.home = os.path.realpath(Path.home())
        self.system = _system_dirs()

    # ---- classification
    def is_protected(self, real: str) -> bool:
        return any(within(real, p) for p in self.protected)

    def is_blocked(self, real: str) -> bool:
        if self.is_protected(real):
            return True
        parts = [x for x in real.lower().replace("\\", "/").split("/") if x]
        joined = "/" + "/".join(parts) + "/"
        if any(f"/{b}/" in joined for b in _BLOCK_PARTS):
            return True
        name = parts[-1] if parts else ""
        return name in _BLOCK_NAMES or os.path.splitext(name)[1] in _BLOCK_EXTS

    def is_system(self, real: str) -> bool:
        return any(within(real, s) for s in self.system)

    def is_precious(self, real: str) -> bool:
        """Things that may not be deleted or moved away: drive roots, the home folder, trusted folders, system folders."""
        n = _norm(real)
        if os.path.dirname(real) == real or n == _norm(self.home) or n in {_norm(t) for t in self.trusted}:
            return True
        if n in {_norm(os.path.join(self.home, d)) for d in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")}:
            return True
        return any(_norm(s) == n for s in self.system)

    def read_free(self, real: str) -> bool:
        """May Hearth read this content without asking? (trusted folder, or allowed earlier in this session)"""
        if self.is_blocked(real):
            return False
        return (any(within(real, t) for t in self.trusted) or any(within(real, d) for d in self.session_dirs)
                or _norm(real) in {_norm(f) for f in self.session_files})

    def readable_roots(self) -> list[str]:
        return dedupe([*self.trusted, *self.session_dirs])

    # ---- grants (these only ever come from the user's own answer or their own click)
    def grant_dir(self, real: str) -> None:
        if real not in self.session_dirs:
            self.session_dirs.append(real)

    def grant_file(self, real: str) -> None:
        self.session_files.add(real)

    # ---- paths
    def resolve(self, raw: str, *, must_exist: bool = True, prefer: tuple[str, ...] | None = None) -> Path:
        """Turn what the model (or user) wrote into a real absolute path, or raise AccessError."""
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            raise AccessError("empty or invalid path")
        text = os.path.expanduser(raw.strip().strip("\"'").strip())
        if not text:
            raise AccessError("empty or invalid path")
        if len(text) == 2 and text[1] == ":" and text[0].isalpha():
            text += "\\"                      # "E:" means the drive itself, not "the current folder on E"
        if os.path.isabs(text):
            chosen = os.path.abspath(text)
        else:   # relative: the first trusted folder that already has it, else the first trusted folder, else the home folder
            bases = [*self.trusted, self.home] if self.trusted else [self.home]
            hits = [os.path.abspath(os.path.join(b, text)) for b in bases if os.path.lexists(os.path.join(b, text))]
            if not hits:   # a new file inside a folder that exists somewhere ("Downloads/new.docx" -> the home Downloads)
                hits = [os.path.abspath(os.path.join(b, text)) for b in bases if os.path.isdir(os.path.dirname(os.path.join(b, text)))]
            chosen = hits[0] if hits else os.path.abspath(os.path.join(bases[0], text))
        real = os.path.realpath(chosen)
        if self.is_protected(real):
            raise AccessError("that is Hearth's own data folder, which is never opened")
        if self.is_blocked(real):
            raise AccessError("that place holds passwords, keys or browser data; Hearth never opens it")
        if os.path.islink(chosen):
            raise AccessError(f"'{raw}' is a link/shortcut; those are not followed")
        if must_exist and not os.path.exists(real):
            return self._by_name(raw, text, prefer)
        return Path(real)

    def _by_name(self, raw: str, text: str, prefer: tuple[str, ...] | None = None) -> Path:
        """The named file is not where the path says: the model wrote a bare name, the wrong drive, or a name without its
        extension ('second run' for 'second run.docx'). Look in the folder the path names first, then the usual places
        (home, Desktop, Documents, Downloads, trusted folders, drives). Exactly one match is used. With several, the
        kinds the caller can work with (prefer, e.g. ('.docx',)) decide; if that is still not one, ask for the full path."""
        parts = re.split(r"[\\/]", text.rstrip("\\/"))
        name = parts[-1]
        if not name or name in (".", ".."):
            raise AccessError(f"'{raw}' does not exist")
        given = os.path.dirname(os.path.abspath(text)) if os.path.isabs(text) else ""
        want = name.lower()
        for folders in ([given] if given and os.path.isdir(given) else [], [f for _, f in self.places()]):
            found: dict[str, str] = {}
            for folder in folders:
                try:
                    entries = os.listdir(folder)
                except OSError:
                    continue
                for entry in entries:
                    full = os.path.join(folder, entry)
                    exact = entry.lower() == want
                    same_stem = os.path.splitext(entry)[0].lower() == want and not os.path.isdir(full)
                    if (exact or same_stem) and not os.path.islink(full):
                        real = os.path.realpath(full)
                        if not self.is_protected(real) and not self.is_blocked(real):
                            found[_norm(real)] = real
            if prefer and len(found) > 1:
                narrowed = {k: v for k, v in found.items() if os.path.splitext(v)[1].lower() in prefer}
                found = narrowed or found
            if len(found) == 1:
                return Path(next(iter(found.values())))
            if len(found) > 1:
                raise AccessError(f"'{raw}' does not exist as written, and several files match '{name}': "
                                  + "; ".join(sorted(found.values())) + ". Use the full path, with the extension, of the one you mean")
        raise AccessError(f"'{raw}' does not exist")

    def check_writable(self, real: str) -> None:
        if self.is_system(real):
            raise AccessError("that is an operating-system folder; Hearth reads there only with permission and never changes it")
        if self.is_blocked(real):
            raise AccessError("that place holds passwords, keys or browser data; Hearth never changes it")

    def places(self) -> list[tuple[str, str]]:
        """Starting points for 'what is on this computer?': home, the usual folders, trusted folders and drives."""
        out: list[tuple[str, str]] = [("Home", self.home)]
        for name in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"):
            p = os.path.join(self.home, name)
            if os.path.isdir(p):
                out.append((name, p))
        out += [("Trusted folder", t) for t in self.trusted if os.path.isdir(t)]
        out += [("Drive", d) for d in drives()]
        seen: set[str] = set()
        uniq: list[tuple[str, str]] = []
        for label, p in out:
            if _norm(p) not in seen:
                seen.add(_norm(p))
                uniq.append((label, p))
        return uniq
