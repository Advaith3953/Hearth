"""Undo journal. Deleted things go to a trash folder, overwritten files are backed up, and every change is recorded."""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any


class UndoError(Exception):
    """An undo that could not be done. `stale` means it never can be (the thing is gone), so it is skipped next time."""

    def __init__(self, message: str, stale: bool = False) -> None:
        super().__init__(message)
        self.stale = stale


class Journal:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.file = base / "journal.json"
        self.trash = base / "trash"
        self.backups = base / "backups"

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(items[-500:], indent=1), encoding="utf-8")
        os.replace(tmp, self.file)

    def record(self, op: str, **data: Any) -> str:
        items = self._load()
        entry_id = uuid.uuid4().hex[:12]
        items.append({"id": entry_id, "ts": time.time(), "op": op, "undone": False, **data})
        self._save(items)
        return entry_id

    def trash_slot(self) -> Path:
        slot = self.trash / uuid.uuid4().hex[:12]
        slot.mkdir(parents=True, exist_ok=True)
        return slot

    def backup(self, src: str) -> str:
        self.backups.mkdir(parents=True, exist_ok=True)
        dst = self.backups / uuid.uuid4().hex[:12]
        shutil.copyfile(src, dst)  # not copy2: the backup's age is when it was made, not the file's age
        return str(dst)

    def next_undo(self) -> dict[str, Any] | None:
        """The change that undo would reverse now, or None."""
        return next((e for e in reversed(self._load()) if not e.get("undone")), None)

    def recent(self, n: int = 10) -> list[dict[str, Any]]:
        return self._load()[-n:][::-1]

    def undo(self) -> str:
        """Reverse the most recent change that has not been undone. Returns a sentence describing what happened."""
        items = self._load()
        for entry in reversed(items):
            if entry.get("undone"):
                continue
            try:
                msg = self._reverse(entry)
            except UndoError as e:
                if e.stale:
                    entry["undone"] = True  # nothing left to restore: do not let it block older changes
                    self._save(items)
                raise
            entry["undone"] = True
            self._save(items)
            return msg
        raise UndoError("there is nothing to undo")

    def _reverse(self, e: dict[str, Any]) -> str:
        op = e["op"]
        if op == "batch":                                  # many changes made by one request: undone together, last first
            items = e["items"]
            done, problems = 0, []
            for item in reversed(items):
                try:
                    self._reverse(item)
                    done += 1
                except UndoError as err:
                    problems.append(str(err))
            if not done:
                raise UndoError(problems[0] if problems else "nothing to undo", stale=True)
            return f"Undid {done} of {len(items)} changes" + (f" ({len(problems)} could not be undone: {problems[0]})" if problems else "")
        if op in ("create", "copy"):
            path = e["path"]
            if not os.path.lexists(path):
                raise UndoError(f"{path} is already gone", stale=True)
            slot = self.trash_slot()
            shutil.move(path, str(slot / os.path.basename(path)))
            return f"Removed {path} (kept in Hearth's trash)"
        if op == "edit":
            path, backup = e["path"], e["backup"]
            if not os.path.exists(backup):
                raise UndoError("the backup of the earlier version is missing", stale=True)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, path)
            return f"Restored the earlier version of {path}"
        if op == "move":
            src, dst = e["src"], e["dst"]
            if not os.path.lexists(dst):
                raise UndoError(f"{dst} is no longer there", stale=True)
            if os.path.lexists(src):
                raise UndoError(f"something else now exists at {src}")
            Path(src).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dst, src)
            return f"Moved {dst} back to {src}"
        if op == "delete":
            path, trashed = e["path"], e["trashed"]
            if os.path.lexists(path):
                raise UndoError(f"something else now exists at {path}")
            if not os.path.lexists(trashed):
                raise UndoError("the deleted item is no longer in the trash", stale=True)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(trashed, path)
            return f"Restored {path} from the trash"
        raise UndoError(f"unknown operation '{op}'")

    def purge(self, days: int = 30) -> int:
        """Remove trash and backups older than `days`. Returns how many items were removed."""
        cutoff = time.time() - days * 86400
        removed = 0
        for d in (self.trash, self.backups):
            if not d.is_dir():
                continue
            for child in d.iterdir():
                if child.stat().st_mtime < cutoff:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                    removed += 1
        return removed
