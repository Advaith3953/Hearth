from __future__ import annotations

import os
from pathlib import Path

import pytest

from hearth.access import Access
from hearth.config import Config
from hearth.index import Index
from hearth.journal import Journal
from hearth.tools import Ctx


@pytest.fixture
def hh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "hearth-home"
    monkeypatch.setenv("HEARTH_HOME", str(h))
    return h


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "shared"
    (r / "docs").mkdir(parents=True)
    (r / "notes.txt").write_text("Rent is due on the 1st.\nSam's phone: 555-0142\n", encoding="utf-8")
    (r / "docs" / "lease.md").write_text("# Lease\n\nThe deposit is 1200 dollars.\n\nPets are not allowed.\n", encoding="utf-8")
    (r / "docs" / "todo.txt").write_text("buy milk\nbuy eggs\n", encoding="utf-8")
    return r


class Asker:
    """A confirm callback that records every question and answers from a policy."""

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str]] = []
        self.choice: str | None = None   # for permission questions: the option to pick ("Allow this file", ...)

    def __call__(self, title: str, detail: str) -> bool:
        self.asked.append((title, detail))
        return self.answer

    def pick(self, title: str, detail: str, options: list[str]) -> int:
        self.asked.append((title, detail))
        if self.choice is not None and self.choice in options:
            return options.index(self.choice)
        return 0 if self.answer else len(options) - 1


@pytest.fixture
def asker() -> Asker:
    return Asker(True)


@pytest.fixture
def ctx(root: Path, hh: Path, asker: Asker) -> Ctx:
    box = Access([root], protected=[hh])
    opened: list[str] = []
    c = Ctx(box, Index(hh / "index.db", box), Journal(hh), asker, opener=opened.append, choose=asker.pick)
    c.opened = opened  # type: ignore[attr-defined]
    return c


def cfg_for(root: Path) -> Config:
    return Config(folders=[str(root)])


def names(path: Path) -> set[str]:
    return {p.name for p in path.iterdir()} if path.is_dir() else set()


__all__ = ["Asker", "cfg_for", "names", "os"]
