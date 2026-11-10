"""Local web page (http://127.0.0.1:7083). Listens on this computer only, rejects requests whose Host/Origin is not
itself (DNS-rebinding and cross-site protection) and needs a custom header on every POST. Changes show up as
approval cards; nothing is changed until you press Allow."""
from __future__ import annotations

import contextlib
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs

from . import docxio, finder, readers, sheets
from .access import AccessError
from .agent import Agent, remember
from .app import make_agent, make_ctx
from .config import Config
from .journal import UndoError
from .model import Model, ModelError

STATIC = Path(__file__).parent / "static"
CONFIRM_TIMEOUT = 600.0
PREVIEW_ROWS = 40
PREVIEW_CHARS = 6000
MAX_COLS = 14


class Session:
    def __init__(self, cfg: Config, auto_safe: bool = False, model: Model | None = None) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.pending: dict[int, dict[str, Any]] = {}
        self.busy = False
        self.shown: tuple[str, ...] = ()
        self.history: list[dict[str, str]] = []
        self.ctx = make_ctx(cfg, self._confirm, auto_safe)
        self.ctx.choose = self._ask
        self.agent: Agent = make_agent(cfg, self.ctx, model, self._on_event)

    def add(self, kind: str, text: str, **extra: Any) -> int:
        with self.lock:
            i = len(self.events)
            self.events.append({"i": i, "kind": kind, "text": text, **extra})
            return i

    def _on_event(self, kind: str, text: str) -> None:
        if kind == "tool":
            self.add("tool", text)
        elif kind == "result" and tuple(self.ctx.found) != self.shown:
            self.show_files(self.ctx.found, self.ctx.found_why)

    # ---- files: cards, selection, preview, direct search (no model needed)

    def file_card(self, path: str, why: str = "") -> dict[str, str]:
        return {"path": path, "name": os.path.basename(path), "folder": os.path.dirname(path),
                "kind": finder.kind_of(path), "why": why}

    def show_files(self, paths: list[str], why: dict[str, str]) -> None:
        self.shown = tuple(paths)
        n = len(paths)
        self.add("files", f"Found {n} file{'s' if n != 1 else ''}. Select one to work on it." if n else "No files found.",
                 files=[self.file_card(p, why.get(p, "")) for p in paths])

    def find(self, query: str, kind: str) -> int:
        norm = finder.normalize_kind(kind)
        if norm is None:
            raise ValueError("kind must be one of: " + ", ".join(finder.KINDS))
        kind = norm
        roots = self.ctx.access.readable_roots()
        if not roots:
            raise ValueError("no folder is open for reading yet. Ask Hearth in the chat to look somewhere and you will be asked")
        self.ctx.index.sync()
        found, _cut = finder.search(self.ctx, query, kind, roots)
        self.ctx.found = [f.path for f in found]
        self.ctx.found_why = {f.path: f.why for f in found}
        self.add("note", f"Find: {query}" + (f" ({kind})" if kind else ""))
        self.show_files(self.ctx.found, self.ctx.found_why)
        return len(found)

    def select(self, path: str | None) -> dict[str, Any]:
        if self.busy:
            raise ValueError("wait until the current request is finished")
        if not path:
            self.ctx.selected = None
            return {"selected": None}
        p = str(self.ctx.access.resolve(path))
        if os.path.isdir(p):
            raise ValueError("pick a file, not a folder")
        self.ctx.access.grant_file(p)    # the user's own click on Select is their permission to read this file
        self.ctx.selected = p
        self.ctx.focus = {} if self.ctx.focus.get("path") != p else self.ctx.focus
        return {"selected": self.file_card(p)}

    def preview(self, path: str) -> dict[str, Any]:
        p = str(self.ctx.access.resolve(path))
        if os.path.isdir(p):
            raise ValueError("that is a folder")
        card = self.file_card(p)
        if not self.ctx.access.read_free(p):
            return {**card, "error": "Hearth has not been allowed to read this file. Press Select to allow it."}
        ext = readers.ext(p)
        try:
            if ext in sheets.SHEET_EXTS:
                focus = set(self.ctx.focus.get("rows", [])) if self.ctx.focus.get("path") == p else set()
                books = sheets.load(p)
                out = []
                for sh in books[:6]:
                    cols = list(range(1, min(sh.max_col, MAX_COLS) + 1))
                    rows = sh.rows
                    show = rows[:PREVIEW_ROWS]
                    extra = [r for r in sorted(focus) if r in sh.cells and r not in show] if sh.name == self.ctx.focus.get("sheet") else []
                    out.append({"name": sh.name, "total": max(len(rows) - 1, 0), "cols": [sheets.col_letters(c) for c in cols],
                                "rows": [{"n": r, "cells": [sh.get(r, c)[:80] for c in cols],
                                          "hit": r in focus and sh.name == self.ctx.focus.get("sheet")} for r in show + extra]})
                return {**card, "table": out}
            text = "\n".join(docxio.paragraph_texts(docxio.load(p))) if ext == ".docx" else readers.read_text(p)
        except (sheets.SheetError, docxio.DocxError, readers.UnreadableError) as e:
            return {**card, "error": str(e)}
        return {**card, "text": text[:PREVIEW_CHARS], "more": len(text) > PREVIEW_CHARS}

    def _ask(self, title: str, detail: str, options: list[str]) -> int:
        """Show a question card with one button per option and wait for the user's click; returns the option's index."""
        waiter = threading.Event()
        slot: dict[str, Any] = {"event": waiter, "choice": len(options) - 1, "n": len(options)}
        i = self.add("confirm", title, detail=detail, options=options)
        with self.lock:
            self.pending[i] = slot
        waiter.wait(CONFIRM_TIMEOUT)
        with self.lock:
            self.pending.pop(i, None)
        self.add("note", options[slot["choice"]] + ".")
        return int(slot["choice"])

    def _confirm(self, title: str, detail: str) -> bool:
        return self._ask(title, detail, ["Allow", "Don't allow"]) == 0

    def answer_confirm(self, i: int, choice: int) -> bool:
        with self.lock:
            slot = self.pending.get(i)
        if slot is None or not 0 <= choice < slot["n"]:
            return False
        slot["choice"] = choice
        slot["event"].set()
        return True

    def start(self, text: str) -> bool:
        with self.lock:
            if self.busy:
                return False
            self.busy = True
        self.add("user", text)
        threading.Thread(target=self._turn, args=(text,), daemon=True).start()
        return True

    def _turn(self, text: str) -> None:
        try:
            self.ctx.index.sync()
            res = self.agent.run(text, self.history)
            remember(self.history, text, res)
            self.add("answer", res.answer)
        except ModelError as e:
            self.add("error", f"Problem with the language model: {e}")
        except Exception as e:  # never let a bug kill the page; show it
            self.add("error", f"Unexpected problem: {type(e).__name__}: {e}")
        finally:
            with self.lock:
                self.busy = False

    def undo(self) -> str:
        try:
            msg = self.ctx.journal.undo()
            self.ctx.index.sync()
        except UndoError as e:
            msg = f"Cannot undo: {e}"
        self.add("note", msg)
        return msg

    def snapshot(self, since: int) -> dict[str, Any]:
        with self.lock:
            return {"events": self.events[since:], "busy": self.busy, "n": len(self.events)}


def make_handler(session: Session) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "HearthUI"

        def log_message(self, *a: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj).encode())

        def _guard(self) -> bool:
            port = cast(tuple[str, int], self.server.server_address)[1]
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self._json(403, {"error": "bad Host header"})
                return False
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if (origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")) \
                        or "X-Hearth-UI" not in self.headers:
                    self._json(403, {"error": "cross-site request refused"})
                    return False
            return True

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= 20000:
                raise ValueError("bad Content-Length")
            obj = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(obj, dict):
                raise ValueError("body must be a JSON object")
            return obj

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            path, _, query = self.path.partition("?")
            if path == "/api/preview":
                try:
                    want = parse_qs(query).get("path", [""])[0]
                    self._json(200, session.preview(want))
                except (ValueError, AccessError, OSError) as e:
                    self._json(400, {"error": str(e)})
                return
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                sel = session.ctx.selected
                self._json(200, {"folders": session.ctx.access.trusted, "allowed": session.ctx.access.readable_roots(), "model": session.cfg.model,
                                 "selected": session.file_card(sel) if sel else None, "kinds": list(finder.KINDS)})
            elif path == "/api/poll":
                since = int(query.removeprefix("since=")) if query.removeprefix("since=").isdigit() else 0
                self._json(200, session.snapshot(since))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            try:
                body = self._body()
                if self.path == "/api/chat":
                    text = str(body.get("text", "")).strip()
                    if not text or len(text) > 4000:
                        raise ValueError("write a request (up to 4000 characters)")
                    if not session.start(text):
                        self._json(409, {"error": "still working on the previous request"})
                        return
                    self._json(200, {"ok": True})
                elif self.path == "/api/confirm":
                    choice = int(body["choice"]) if "choice" in body else (0 if body.get("ok") else 1)
                    ok = session.answer_confirm(int(body.get("id", -1)), choice)
                    self._json(200 if ok else 404, {"ok": ok})
                elif self.path == "/api/find":
                    query = str(body.get("query", "")).strip()
                    if not query or len(query) > 200:
                        raise ValueError("write what to look for (up to 200 characters)")
                    self._json(200, {"count": session.find(query, str(body.get("kind", "")).lower())})
                elif self.path == "/api/select":
                    self._json(200, session.select(str(body["path"]) if body.get("path") else None))
                elif self.path == "/api/undo":
                    self._json(200, {"message": session.undo()})
                else:
                    self._json(404, {"error": "not found"})
            except (ValueError, TypeError, AccessError, OSError) as e:
                self._json(400, {"error": str(e)})

    return Handler


def serve(cfg: Config, port: int, open_browser: bool, auto_safe: bool) -> int:
    session = Session(cfg, auto_safe)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(session))
    except OSError as e:
        print(f"Cannot listen on 127.0.0.1:{port}: {e}")
        return 1
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Hearth is running at {url}  (press Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()
    return 0
