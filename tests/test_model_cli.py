from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from conftest import cfg_for

from hearth import cli
from hearth.app import check_grantable
from hearth.config import Config
from hearth.model import ModelError, OllamaModel, ScriptedModel, check_local_url


@pytest.fixture
def fake_ollama():
    seen: list[dict] = []
    state = {"status": 200, "body": {"message": {"role": "assistant", "content": '{"answer":"hi"}'}}}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            self._send({"models": [{"name": "llama3.1:8b"}]})

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self._send(state["body"], state["status"])

        def _send(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen, state
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("url", ["http://example.com", "https://127.0.0.1:11434", "http://192.168.1.5:11434", "http://10.0.0.2", "ftp://localhost"])
def test_only_this_computer_is_ever_contacted(url: str):
    with pytest.raises(ValueError, match="this computer"):
        OllamaModel("m", url)
    with pytest.raises(ValueError):
        check_local_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://localhost:11434/", "http://[::1]:11434"])
def test_loopback_urls_are_accepted(url: str):
    assert OllamaModel("m", url).url.startswith("http://")


def test_chat_request_shape_and_proxy_bypass(fake_ollama, monkeypatch: pytest.MonkeyPatch):
    url, seen, _ = fake_ollama
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")            # a dead proxy must not matter
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    m = OllamaModel("llama3.1:8b", url)
    assert m.chat([{"role": "user", "content": "x"}]) == '{"answer":"hi"}'
    assert seen[0]["format"] == "json" and seen[0]["stream"] is False and seen[0]["model"] == "llama3.1:8b"
    assert m.installed() == ["llama3.1:8b"]


def test_model_errors_are_readable(fake_ollama):
    url, _, state = fake_ollama
    m = OllamaModel("x", url)
    state.update(status=404, body={"error": "model 'x' not found"})
    with pytest.raises(ModelError, match="404"):
        m.chat([])
    state.update(status=200, body={"error": "boom"})
    with pytest.raises(ModelError, match="boom"):
        m.chat([])
    state.update(status=200, body=[1])
    with pytest.raises(ModelError):
        m.chat([])
    with pytest.raises(ModelError, match="Is it running"):
        OllamaModel("x", "http://127.0.0.1:9", timeout=2).chat([])


def test_scripted_model_runs_out():
    m = ScriptedModel(["a"])
    assert m.chat([]) == "a"
    with pytest.raises(ModelError):
        m.chat([])


# ------------------------------------------------------------------ CLI

def test_grant_revoke_folders(hh: Path, root: Path, capsys):
    assert cli.main(["folders"]) == 0 and "no trusted folders" in capsys.readouterr().out
    assert cli.main(["grant", str(root)]) == 0
    assert Config.load().folders == [str(root.resolve())]
    cli.main(["grant", str(root)])
    assert len(Config.load().folders) == 1                             # idempotent
    assert cli.main(["revoke", str(root)]) == 0 and Config.load().folders == []
    assert cli.main(["revoke", str(root)]) == 1


def test_dangerous_grants_are_refused(hh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(ValueError, match="entire drive"):
        check_grantable("/")
    with pytest.raises(ValueError, match="whole home folder"):
        check_grantable(str(tmp_path))
    assert check_grantable(str(tmp_path), force=True)
    hh.mkdir()
    with pytest.raises(ValueError, match="own data"):
        check_grantable(str(hh))
    with pytest.raises(ValueError, match="not a folder"):
        check_grantable(str(tmp_path / "missing"))
    assert cli.main(["grant", "/"]) == 2


def test_ask_runs_one_turn(hh: Path, root: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    Config(folders=[str(root)]).save()
    model = ScriptedModel([json.dumps({"tool": "read_file", "args": {"path": "notes.txt"}}), json.dumps({"answer": "Rent is due on the 1st."})])
    real = cli.make_agent
    monkeypatch.setattr(cli, "make_agent", lambda cfg, ctx, m=None, on_event=None: real(cfg, ctx, model, on_event))
    assert cli.main(["ask", "when is rent due?"]) == 0
    out = capsys.readouterr().out
    assert "Reading notes.txt" in out and "Rent is due" in out


def test_ask_without_trusted_folders_still_runs_and_reports_model_down(hh: Path, root: Path, capsys):
    assert cli.main(["ask", "hi"]) == 3 and "language model" in capsys.readouterr().out     # no folder needed any more
    Config(folders=[str(root)], ollama_url="http://127.0.0.1:9").save()
    assert cli.main(["ask", "hi"]) == 3 and "language model" in capsys.readouterr().out


def test_confirm_without_a_keyboard_says_no(monkeypatch: pytest.MonkeyPatch):
    class NoTty:
        def isatty(self): return False
    monkeypatch.setattr("sys.stdin", NoTty())
    assert cli.cli_confirm("Delete x?", "detail") is False


def test_confirm_with_keyboard(monkeypatch: pytest.MonkeyPatch):
    class Tty:
        def isatty(self): return True
    monkeypatch.setattr("sys.stdin", Tty())
    monkeypatch.setattr("builtins.input", lambda _="": "y")
    assert cli.cli_confirm("t", "d") is True
    monkeypatch.setattr("builtins.input", lambda _="": "n")
    assert cli.cli_confirm("t", "d") is False


def test_chat_loop(hh: Path, root: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    Config(folders=[str(root)]).save()
    model = ScriptedModel([json.dumps({"answer": "Hello!"})])
    real = cli.make_agent
    monkeypatch.setattr(cli, "make_agent", lambda cfg, ctx, m=None, on_event=None: real(cfg, ctx, model, on_event))
    lines = iter(["hi", "", "undo", "quit"])
    monkeypatch.setattr("builtins.input", lambda _="": next(lines))
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "Hello!" in out and "nothing to undo" in out


def test_history_undo_index_cleanup_model(hh: Path, root: Path, capsys):
    Config(folders=[str(root)]).save()
    assert cli.main(["history"]) == 0 and "no changes" in capsys.readouterr().out
    assert cli.main(["undo"]) == 1
    assert cli.main(["index"]) == 0 and "3 file" in capsys.readouterr().out
    from hearth.app import make_ctx
    c = make_ctx(cfg_for(root), lambda t, d: True)
    from hearth.tools import run_tool
    run_tool(c, "write_file", {"path": "x.txt", "content": "x"})
    run_tool(c, "move_path", {"source": "x.txt", "destination": "y.txt"})
    assert cli.main(["history"]) == 0 and "move" in capsys.readouterr().out
    assert cli.main(["undo"]) == 0 and (root / "x.txt").exists()
    assert cli.main(["cleanup", "--days", "0"]) == 0
    assert cli.main(["model", "qwen2.5:7b"]) == 0 and Config.load().model == "qwen2.5:7b"


def test_doctor(hh: Path, root: Path, fake_ollama, capsys, monkeypatch):
    url, _, _ = fake_ollama
    Config(folders=[str(root)], ollama_url=url).save()
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "FTS5" in out and "is installed" in out and "PDF reading (pypdf)" in out
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, *a: None if n == "pypdf" else real(n, *a))
    cli.main(["doctor"])
    assert "pip install pypdf" in capsys.readouterr().out
    monkeypatch.setattr(importlib.util, "find_spec", real)
    Config(folders=[], model="nonexistent", ollama_url=url).save()
    assert cli.main(["doctor"]) == 1 and "ollama pull nonexistent" in capsys.readouterr().out
    Config(folders=[str(root)], ollama_url="http://127.0.0.1:9").save()
    assert cli.main(["doctor"]) == 1


def test_config_survives_garbage(hh: Path):
    hh.mkdir()
    (hh / "config.json").write_text("{not json")
    assert Config.load().folders == []
    (hh / "config.json").write_text('["list"]')
    assert Config.load().model == "llama3.1:8b"
    (hh / "config.json").write_text('{"folders": ["a", 3], "model": "  ", "max_steps": 999, "ollama_url": 5}')
    c = Config.load()
    assert c.folders == ["a"] and c.model == "llama3.1:8b" and c.max_steps == 8


def test_cli_choose_prompt(monkeypatch: pytest.MonkeyPatch, capsys):
    opts = ["Allow this file", "Allow this folder", "Don't allow"]

    class Tty:
        def isatty(self): return True
    monkeypatch.setattr("sys.stdin", Tty())
    for typed, want in (("1", 0), ("2", 1), ("", 2), ("9", 2), ("x", 2)):
        monkeypatch.setattr("builtins.input", lambda prompt="", t=typed: t)
        assert cli.cli_choose("Allow Hearth to read a.txt?", "C:\\a.txt\nwhy", opts) == want
    assert "1) Allow this file" in capsys.readouterr().out

    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert cli.cli_choose("t", "d", opts) == 2

    class NoTty:
        def isatty(self): return False
    monkeypatch.setattr("sys.stdin", NoTty())
    assert cli.cli_choose("t", "d", opts) == 2


def test_a_repeat_loop_abort_is_retried_once_with_more_randomness(monkeypatch: pytest.MonkeyPatch):
    m = OllamaModel("x")
    bodies = []

    def fake(path, body=None, timeout=None):
        bodies.append(body["options"])
        if len(bodies) == 1:
            raise ModelError('the model server answered 500: {"error":"prediction aborted, token repeat limit reached"}')
        return {"message": {"content": '{"answer": "ok"}'}}
    monkeypatch.setattr(m, "_call", fake)
    assert m.chat([]) == '{"answer": "ok"}'
    assert bodies[0]["temperature"] == 0.1 and bodies[1]["temperature"] > 0.1 and bodies[1]["repeat_penalty"] > 1.0

    def other(path, body=None, timeout=None):
        raise ModelError("the model server answered 500: out of memory")
    monkeypatch.setattr(m, "_call", other)
    with pytest.raises(ModelError, match="out of memory"):
        m.chat([])
