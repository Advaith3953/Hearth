"""Wiring: build the access policy, index, journal and tools from the saved settings."""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from .access import Access
from .agent import Agent, Event
from .config import Config, home
from .index import Index
from .journal import Journal
from .model import Model, OllamaModel
from .tools import Ctx


def make_ctx(cfg: Config, confirm: Callable[[str, str], bool], auto_safe: bool = False,
             base: Path | None = None) -> Ctx:
    base = base or home()
    box = Access([f for f in cfg.folders if os.path.isdir(f)], protected=[base])
    return Ctx(box, Index(base / "index.db", box), Journal(base), confirm, auto_approve_safe=auto_safe)


def make_agent(cfg: Config, ctx: Ctx, model: Model | None = None, on_event: Event | None = None) -> Agent:
    return Agent(model or OllamaModel(cfg.model, cfg.ollama_url), ctx, cfg.max_steps, on_event)


def check_grantable(path: str, force: bool = False) -> str:
    """Validate a folder the user wants to trust (read without asking); returns its real path or raises ValueError."""
    real = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(real):
        raise ValueError(f"'{path}' is not a folder")
    if os.path.dirname(real) == real:
        raise ValueError("refusing to trust an entire drive; choose a specific folder")
    user_home = os.path.realpath(Path.home())
    if real == user_home and not force:
        raise ValueError("that is your whole home folder, including settings and keys. Trust a subfolder such as "
                         "Documents instead (or add --force if you really mean it)")
    hh = os.path.realpath(home())
    if real == hh or real.startswith(hh + os.sep):
        raise ValueError("Hearth's own data folder cannot be opened")
    probe = Access()
    if probe.is_blocked(real):
        raise ValueError("that place holds passwords, keys or browser data; Hearth never opens it")
    if probe.is_system(real):
        raise ValueError("that is an operating-system folder; Hearth will ask each time instead of trusting it")
    return real
