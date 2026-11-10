"""Command line: hearth grant/revoke/ask/chat/ui/undo/..."""
from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import sys

from . import __version__
from .agent import remember
from .app import check_grantable, make_agent, make_ctx
from .config import Config, home
from .journal import UndoError
from .model import ModelError, OllamaModel


def cli_confirm(title: str, detail: str) -> bool:
    print(f"\n  ? {title}")
    for line in detail.splitlines():
        print(f"      {line}")
    if not sys.stdin or not sys.stdin.isatty():
        print("  (no keyboard attached: answering no)")
        return False
    try:
        return input("  Allow? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cli_choose(title: str, detail: str, options: list[str]) -> int:
    print(f"\n  ? {title}")
    for line in detail.splitlines():
        print(f"      {line}")
    for n, opt in enumerate(options, 1):
        print(f"      {n}) {opt}")
    if not sys.stdin or not sys.stdin.isatty():
        print("  (no keyboard attached: answering no)")
        return len(options) - 1
    try:
        pick = input(f"  Choose 1-{len(options)} [{len(options)}] ").strip()
    except EOFError:
        return len(options) - 1
    return int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= len(options) else len(options) - 1


def _events(kind: str, text: str) -> None:
    if kind == "tool":
        print(f"  · {text}")


def cmd_grant(cfg: Config, a: argparse.Namespace) -> int:
    try:
        real = check_grantable(a.folder, a.force)
    except ValueError as e:
        print(f"Not trusted: {e}")
        return 2
    if real not in cfg.folders:
        cfg.folders.append(real)
        cfg.save()
    print(f"Trusted: {real}\nHearth will read files in there without asking. It still asks before changing anything, "
          "and asks before reading files anywhere else.")
    return 0


def cmd_revoke(cfg: Config, a: argparse.Namespace) -> int:
    real = os.path.realpath(os.path.expanduser(a.folder))
    if real not in cfg.folders:
        print("That folder is not trusted.")
        return 1
    cfg.folders.remove(real)
    cfg.save()
    print(f"No longer trusted: {real}")
    return 0


def cmd_folders(cfg: Config, a: argparse.Namespace) -> int:
    print("\n".join(cfg.folders) if cfg.folders else "(no trusted folders: Hearth asks before reading anything)")
    return 0


def _run_turns(cfg: Config, a: argparse.Namespace, prompts: list[str] | None) -> int:
    ctx = make_ctx(cfg, cli_confirm, auto_safe=a.auto_safe)
    ctx.choose = cli_choose
    agent = make_agent(cfg, ctx, on_event=_events)
    ctx.index.sync()
    history: list[dict[str, str]] = []
    try:
        if prompts is not None:
            for p in prompts:
                res = agent.run(p, history)
                print("\n" + res.answer)
                return 0 if res.ok else 1
        print(f"Hearth {__version__} - model {cfg.model} - type 'quit' to leave, 'undo' to reverse the last change.")
        while True:
            try:
                line = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if line.lower() in ("quit", "exit", "q"):
                return 0
            if line.lower() == "undo":
                _undo(ctx)
                continue
            if line:
                ctx.index.sync()
                res = agent.run(line, history)
                print("\n" + res.answer)
                remember(history, line, res)
    except ModelError as e:
        print(f"\nProblem with the language model: {e}")
        return 3
    return 0


def _undo(ctx) -> int:  # type: ignore[no-untyped-def]
    try:
        print(ctx.journal.undo())
        ctx.index.sync()
        return 0
    except UndoError as e:
        print(f"Cannot undo: {e}")
        return 1


def cmd_undo(cfg: Config, a: argparse.Namespace) -> int:
    return _undo(make_ctx(cfg, cli_confirm))


def cmd_history(cfg: Config, a: argparse.Namespace) -> int:
    import time
    items = make_ctx(cfg, cli_confirm).journal.recent(20)
    for e in items:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
        target = e.get("path") or f"{e.get('src')} -> {e.get('dst')}"
        print(f"{when}  {e['op']:7} {target}{'  (undone)' if e.get('undone') else ''}")
    if not items:
        print("(no changes recorded)")
    return 0


def cmd_index(cfg: Config, a: argparse.Namespace) -> int:
    changed, gone = make_ctx(cfg, cli_confirm).index.sync()
    print(f"Index updated: {changed} file(s) added or refreshed, {gone} removed.")
    return 0


def cmd_cleanup(cfg: Config, a: argparse.Namespace) -> int:
    n = make_ctx(cfg, cli_confirm).journal.purge(a.days)
    print(f"Removed {n} trash/backup item(s) older than {a.days} days.")
    return 0


def cmd_model(cfg: Config, a: argparse.Namespace) -> int:
    cfg.model = a.name
    cfg.save()
    print(f"Model set to {a.name}")
    return 0


def cmd_doctor(cfg: Config, a: argparse.Namespace) -> int:
    ok = True

    def line(good: bool, text: str) -> None:
        nonlocal ok
        ok = ok and good
        print(f"  [{'ok' if good else '!!'}] {text}")

    line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]} (need 3.10+)")
    try:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        finally:
            probe.close()
        line(True, "search index support (SQLite FTS5)")
    except sqlite3.Error:
        line(False, "this Python's SQLite lacks FTS5, so search will not work")
    if importlib.util.find_spec("pypdf") is not None:
        print("  [ok] PDF reading (pypdf)")
    else:
        print("  [--] PDF reading is off (optional). To turn it on: pip install pypdf")
    good = [f for f in cfg.folders if os.path.isdir(f)]
    print(f"  [ok] {len(good)} trusted folder(s)" + ("" if good else " (optional; without one Hearth asks before reading each place)"))
    try:
        m = OllamaModel(cfg.model, cfg.ollama_url)
        names = m.installed()
        have = any(n == cfg.model or n.split(":")[0] == cfg.model for n in names)
        line(True, "Ollama is running on this computer")
        line(have, f"model '{cfg.model}' is installed" if have else
             f"model '{cfg.model}' is not installed - run: ollama pull {cfg.model}")
    except (ModelError, ValueError) as e:
        line(False, str(e))
    print(f"  data folder: {home()}")
    return 0 if ok else 1


def cmd_ui(cfg: Config, a: argparse.Namespace) -> int:
    from .webui import serve
    return serve(cfg, a.port, not a.no_browser, a.auto_safe)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hearth", description="A private, offline assistant for the files on this computer. It lists freely and asks before reading or changing.")
    p.add_argument("--version", action="version", version=f"hearth {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def add(name: str, fn, help_: str):  # type: ignore[no-untyped-def]
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    g = add("grant", cmd_grant, "trust a folder: Hearth reads it without asking")
    g.add_argument("folder")
    g.add_argument("--force", action="store_true", help="allow trusting your whole home folder")
    add("revoke", cmd_revoke, "stop trusting a folder").add_argument("folder")
    add("folders", cmd_folders, "list trusted folders")
    for name, fn, h in (("chat", lambda c, a: _run_turns(c, a, None), "talk to Hearth (default)"),
                        ("ui", cmd_ui, "open the local web page")):
        sp = add(name, fn, h)
        sp.add_argument("--auto-safe", action="store_true",
                        help="do not ask before creating files/folders, copying or moving (deleting and editing still ask)")
        if name == "ui":
            sp.add_argument("--port", type=int, default=7083)
            sp.add_argument("--no-browser", action="store_true")
    ask = add("ask", lambda c, a: _run_turns(c, a, [a.request]), "ask or tell Hearth one thing and exit")
    ask.add_argument("request")
    ask.add_argument("--auto-safe", action="store_true")
    add("undo", cmd_undo, "reverse the most recent change")
    add("history", cmd_history, "recent changes Hearth made")
    add("index", cmd_index, "refresh the search index now")
    add("cleanup", cmd_cleanup, "empty old trash and backups").add_argument("--days", type=int, default=30)
    add("model", cmd_model, "choose the Ollama model").add_argument("name")
    add("doctor", cmd_doctor, "check that everything is set up")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load()
    if not getattr(args, "fn", None):
        args.auto_safe = False
        return _run_turns(cfg, args, None)
    return int(args.fn(cfg, args))


if __name__ == "__main__":
    raise SystemExit(main())
