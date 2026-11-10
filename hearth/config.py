"""Settings and the list of trusted folders, stored in ~/.hearth (or $HEARTH_HOME)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def home() -> Path:
    """Where Hearth keeps its own data (settings, search index, undo journal, trash)."""
    return Path(os.environ.get("HEARTH_HOME") or Path.home() / ".hearth")


@dataclass
class Config:
    folders: list[str] = field(default_factory=list)
    model: str = "llama3.1:8b"
    ollama_url: str = "http://127.0.0.1:11434"
    max_steps: int = 8

    @classmethod
    def load(cls) -> Config:
        cfg = cls()
        try:
            raw = json.loads((home() / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cfg
        if not isinstance(raw, dict):
            return cfg
        if isinstance(raw.get("folders"), list):
            cfg.folders = [f for f in raw["folders"] if isinstance(f, str)]
        if isinstance(raw.get("model"), str) and raw["model"].strip():
            cfg.model = raw["model"].strip()
        if isinstance(raw.get("ollama_url"), str):
            cfg.ollama_url = raw["ollama_url"]
        if isinstance(raw.get("max_steps"), int) and 1 <= raw["max_steps"] <= 30:
            cfg.max_steps = raw["max_steps"]
        return cfg

    def save(self) -> None:
        h = home()
        h.mkdir(parents=True, exist_ok=True)
        tmp = h / "config.json.tmp"
        tmp.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")
        os.replace(tmp, h / "config.json")
