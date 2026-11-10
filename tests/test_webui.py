from __future__ import annotations

import json
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import cfg_for

from hearth.model import ModelError, ScriptedModel
from hearth.webui import Session, make_handler, serve


def raw(port: int, method: str, path: str, body: bytes = b"", headers: dict | None = None, length: str | None = None) -> tuple[int, bytes]:
    h = {"Host": f"127.0.0.1:{port}", "Connection": "close", **(headers or {})}
    if length is not None:
        h["Content-Length"] = length
    elif body:
        h["Content-Length"] = str(len(body))
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items())
    with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
        c.sendall(head.encode() + b"\r\n" + body)
        data = b""
        while chunk := c.recv(65536):
            data += chunk
    status = int(data.split(b" ", 2)[1])
    return status, data.split(b"\r\n\r\n", 1)[1]


def post(port, path, obj, extra=None):
    return raw(port, "POST", path, json.dumps(obj).encode(), {"X-Hearth-UI": "1", **(extra or {})})


def wait_idle(port, since=0, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        _, b = raw(port, "GET", f"/api/poll?since={since}")
        d = json.loads(b)
        if not d["busy"]:
            return d
        time.sleep(0.05)
    raise AssertionError("still busy")


@pytest.fixture
def app(hh: Path, root: Path):
    holder = {}

    def start(replies):
        s = Session(cfg_for(root), model=ScriptedModel(replies))
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(s))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        holder["srv"] = srv
        return s, srv.server_address[1]
    yield start
    if "srv" in holder:
        holder["srv"].shutdown()
        holder["srv"].server_close()


def A(text): return json.dumps({"answer": text})
def T(name, **a): return json.dumps({"tool": name, "args": a})


def test_page_and_state(app):
    _, port = app([])
    st, body = raw(port, "GET", "/")
    assert st == 200 and b"Hearth" in body
    st, body = raw(port, "GET", "/api/state")
    assert json.loads(body)["folders"]
    assert raw(port, "GET", "/nope")[0] == 404


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1:1", "localhost"])
def test_wrong_host_header_is_refused(app, host):
    _, port = app([])
    assert raw(port, "GET", "/api/state", headers={"Host": host})[0] == 403


def test_cross_site_and_missing_header_posts_are_refused(app):
    _, port = app([A("x")])
    assert raw(port, "POST", "/api/chat", b'{"text":"hi"}')[0] == 403                          # no custom header
    assert post(port, "/api/chat", {"text": "hi"}, {"Origin": "http://evil.example"})[0] == 403


@pytest.mark.parametrize("length,body", [("-5", b""), ("abc", b""), ("999999", b"{}"), ("3", b"[1]")])
def test_malformed_bodies_get_400_not_a_hang(app, length, body):
    _, port = app([])
    assert raw(port, "POST", "/api/chat", body, {"X-Hearth-UI": "1"}, length=length)[0] == 400


def test_empty_request_rejected(app):
    _, port = app([])
    assert post(port, "/api/chat", {"text": "   "})[0] == 400
    assert post(port, "/api/nothing", {})[0] == 404


def test_chat_turn_end_to_end(app):
    s, port = app([T("read_file", path="notes.txt"), A("Rent is due on the 1st.")])
    assert post(port, "/api/chat", {"text": "when is rent due?"})[0] == 200
    d = wait_idle(port)
    kinds = [e["kind"] for e in d["events"]]
    assert kinds == ["user", "tool", "answer"] and d["events"][-1]["text"].startswith("Rent")
    assert json.loads(raw(port, "GET", f"/api/poll?since={d['n']}")[1])["events"] == []


def test_approval_card_allow_and_deny(app, root: Path):
    s, port = app([T("delete_path", path="notes.txt"), A("Done."), T("delete_path", path="notes.txt"), A("Left it alone.")])
    post(port, "/api/chat", {"text": "delete notes"})
    deadline = time.time() + 5
    card = None
    while time.time() < deadline and card is None:
        evs = json.loads(raw(port, "GET", "/api/poll?since=0")[1])["events"]
        card = next((e for e in evs if e["kind"] == "confirm"), None)
        time.sleep(0.05)
    assert card and "Delete" in card["text"] and (root / "notes.txt").exists()   # nothing happens before approval
    assert post(port, "/api/confirm", {"id": card["i"], "ok": True})[0] == 200
    wait_idle(port)
    assert not (root / "notes.txt").exists()
    assert post(port, "/api/confirm", {"id": card["i"], "ok": True})[0] == 404         # one answer per card
    assert "Restored" in json.loads(post(port, "/api/undo", {})[1])["message"]
    assert (root / "notes.txt").exists()
    post(port, "/api/chat", {"text": "delete notes again"})
    n = len(json.loads(raw(port, "GET", "/api/poll?since=0")[1])["events"])
    end = time.time() + 5
    while time.time() < end:
        evs = json.loads(raw(port, "GET", "/api/poll?since=0")[1])["events"]
        cards = [e for e in evs if e["kind"] == "confirm"]
        if len(cards) == 2:
            break
        time.sleep(0.05)
    post(port, "/api/confirm", {"id": cards[1]["i"], "ok": False})
    d = wait_idle(port)
    assert (root / "notes.txt").exists() and n > 0
    assert any(e["kind"] == "answer" and "alone" in e["text"] for e in d["events"])


def test_second_request_while_busy_is_refused(app):
    s, port = app([T("delete_path", path="notes.txt"), A("ok")])
    post(port, "/api/chat", {"text": "delete notes"})
    time.sleep(0.3)
    assert post(port, "/api/chat", {"text": "second"})[0] == 409
    for e in s.snapshot(0)["events"]:
        if e["kind"] == "confirm":
            s.answer_confirm(e["i"], False)
    wait_idle(port)


def test_model_failure_is_shown_not_fatal(app):
    s, port = app(lambda msgs: (_ for _ in ()).throw(ModelError("Ollama is not running")))
    post(port, "/api/chat", {"text": "hi"})
    d = wait_idle(port)
    assert d["events"][-1]["kind"] == "error" and "Ollama" in d["events"][-1]["text"]
    s2, port2 = app(lambda msgs: (_ for _ in ()).throw(RuntimeError("bug")))
    post(port2, "/api/chat", {"text": "hi"})
    assert "Unexpected" in wait_idle(port2)["events"][-1]["text"]


def test_undo_with_nothing_to_undo(app):
    _, port = app([])
    assert "nothing to undo" in json.loads(post(port, "/api/undo", {})[1])["message"]


def test_serve_reports_a_busy_port(hh: Path, root: Path, capsys):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    assert serve(cfg_for(root), blocker.getsockname()[1], False, False) == 1
    blocker.close()


# ------------------------------------------------------------------ find / select / preview (new in 0.2)

def _book(root: Path) -> Path:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "People"
    ws.append(["Name", "Age", "Country"])
    for i in range(60):
        ws.append([f"P{i}", i, "India"])
    ws.append(["Jack", 15, "Spain"])
    p = root / "people.xlsx"
    wb.save(p)
    return p


def test_find_bar_returns_file_cards_without_the_model(app, root: Path):
    book = _book(root)
    s, port = app([])
    st, b = post(port, "/api/find", {"query": "Country", "kind": "excel"})
    assert st == 200 and json.loads(b)["count"] == 1
    ev = [e for e in wait_idle(port)["events"] if e["kind"] == "files"][0]
    assert ev["files"][0]["path"] == str(book) and ev["files"][0]["kind"] == "excel" and "Country" in ev["files"][0]["why"]
    assert post(port, "/api/find", {"query": ""})[0] == 400
    assert post(port, "/api/find", {"query": "x" * 201})[0] == 400
    assert post(port, "/api/find", {"query": "x", "kind": "banana"})[0] == 400
    st, b = post(port, "/api/find", {"query": "nothing-matches-zzqq"})
    assert json.loads(b)["count"] == 0 and any(e["text"] == "No files found." for e in wait_idle(port)["events"])


def test_model_find_also_shows_cards(app, root: Path):
    book = _book(root)
    s, port = app([T("search_text", query="Country", kind="excel"), A("Found it.")])
    post(port, "/api/chat", {"text": "get me excel files with Country"})
    d = wait_idle(port)
    cards = [e for e in d["events"] if e["kind"] == "files"]
    assert len(cards) == 1 and cards[0]["files"][0]["path"] == str(book)
    assert any(e["kind"] == "tool" and e["text"] == "Searching inside files for 'Country'" for e in d["events"])


def test_select_then_edit_the_row_through_the_page(app, root: Path):
    book = _book(root)
    s, port = app([T("sheet_find", text="Jack", column="Name"),
                   T("sheet_set_cell", column="Age", value="98", expect="15"), A("Jack is now 98.")])
    st, b = post(port, "/api/select", {"path": str(book)})
    assert st == 200 and json.loads(b)["selected"]["name"] == "people.xlsx" and s.ctx.selected == str(book)
    _, state = raw(port, "GET", "/api/state")
    assert json.loads(state)["selected"]["path"] == str(book)
    post(port, "/api/chat", {"text": "get the row with Jack, set his age to 98"})
    end = time.time() + 10
    card = None
    while time.time() < end and card is None:
        card = next((e for e in json.loads(raw(port, "GET", "/api/poll?since=0")[1])["events"] if e["kind"] == "confirm"), None)
        time.sleep(0.05)
    assert card and "Age: 15  ->  98" in card["detail"]
    post(port, "/api/confirm", {"id": card["i"], "ok": True})
    wait_idle(port)
    import openpyxl
    assert openpyxl.load_workbook(book)["People"]["B62"].value == 98
    st, b = raw(port, "GET", "/api/preview?path=" + str(book).replace("/", "%2F"))
    pv = json.loads(b)
    rows = pv["table"][0]["rows"]
    assert st == 200 and len(rows) == 41                            # first 40 (header included) + the focused row far below
    hit = [r for r in rows if r["hit"]]
    assert hit and hit[0]["n"] == 62 and hit[0]["cells"][:2] == ["Jack", "98"]
    assert pv["table"][0]["total"] == 61


def test_select_guards(app, root: Path, tmp_path: Path):
    s, port = app([])
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    assert not s.ctx.access.read_free(str(outside))
    assert post(port, "/api/select", {"path": str(outside)})[0] == 200      # the user's own click is their permission
    assert s.ctx.access.read_free(str(outside))
    assert post(port, "/api/select", {"path": str(root / "docs")})[0] == 400
    assert post(port, "/api/select", {"path": str(tmp_path / "missing.txt")})[0] == 400
    assert post(port, "/api/select", {"path": "~/.ssh/id_rsa"})[0] == 400   # secret stores can never be selected
    assert post(port, "/api/select", {"path": str(root / "notes.txt")})[0] == 200
    assert json.loads(post(port, "/api/select", {"path": None})[1]) == {"selected": None} and s.ctx.selected is None
    assert raw(port, "POST", "/api/select", b"{}", {})[0] == 403                 # needs the custom header


def test_preview_text_docx_guards_and_errors(app, root: Path, tmp_path: Path):
    from docx import Document
    d = Document()
    d.add_paragraph("Hello from Word")
    d.save(root / "w.docx")
    (root / "bad.xlsx").write_bytes(b"junk")
    (root / "long.txt").write_text("x" * 7000)
    s, port = app([])
    get = lambda p: raw(port, "GET", "/api/preview?path=" + p)   # noqa: E731
    st, b = get(str(root / "w.docx"))
    assert st == 200 and json.loads(b)["text"] == "Hello from Word"
    pv = json.loads(get(str(root / "long.txt"))[1])
    assert len(pv["text"]) == 6000 and pv["more"] is True
    assert "error" in json.loads(get(str(root / "bad.xlsx"))[1])
    other = tmp_path / "other.txt"
    other.write_text("private")
    locked = json.loads(get(str(other))[1])                                  # not allowed yet: no content is shown
    assert "not been allowed" in locked["error"] and "text" not in locked
    assert get(str(tmp_path / "x.txt"))[0] == 400 and get(str(root / "docs"))[0] == 400
    assert get("")[0] == 400
    assert raw(port, "GET", "/api/preview?path=x", headers={"Host": "evil.example"})[0] == 403


def test_cannot_change_selection_while_busy(app, root: Path):
    s, port = app([T("list_folder")] * 2 + [A("x")])
    s.busy = True
    assert post(port, "/api/select", {"path": str(root / "notes.txt")})[0] == 400
    s.busy = False


# ------------------------------------------------------------------ permission questions (v0.3)

def _wait_card(port: int) -> dict:
    end = time.time() + 10
    while time.time() < end:
        card = next((e for e in json.loads(raw(port, "GET", "/api/poll?since=0")[1])["events"] if e["kind"] == "confirm"), None)
        if card:
            return card
        time.sleep(0.05)
    raise AssertionError("no question appeared")


def test_reading_elsewhere_shows_a_three_button_question(app, tmp_path: Path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "diary.txt").write_text("blue heron")
    s, port = app([T("read_file", path=str(other / "diary.txt")), A("It mentions a heron.")])
    post(port, "/api/chat", {"text": "read the diary"})
    card = _wait_card(port)
    assert card["text"] == "Allow Hearth to read diary.txt?" and card["options"] == ["Allow this file", "Allow this folder until Hearth closes", "Don't allow"]
    assert post(port, "/api/confirm", {"id": card["i"], "choice": 7})[0] == 404          # not one of the buttons
    assert post(port, "/api/confirm", {"id": card["i"], "choice": 1})[0] == 200
    wait_idle(port)
    assert s.ctx.access.session_dirs == [str(other.resolve())]


def test_denying_a_read_in_the_page_opens_nothing(app, tmp_path: Path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "diary.txt").write_text("blue heron")
    s, port = app([T("read_file", path=str(other / "diary.txt")), A("You did not allow it.")])
    post(port, "/api/chat", {"text": "read the diary"})
    card = _wait_card(port)
    post(port, "/api/confirm", {"id": card["i"], "choice": 2})
    d = wait_idle(port)
    assert not s.ctx.access.read_free(str(other / "diary.txt"))
    assert any(e["kind"] == "note" and e["text"] == "Don't allow." for e in d["events"])


def test_find_bar_needs_an_open_folder_and_state_lists_trusted_and_allowed(hh: Path):
    from hearth.config import Config
    s = Session(Config(), model=ScriptedModel([]))
    with pytest.raises(ValueError, match="no folder is open"):
        s.find("x", "")
    s.ctx.access.grant_dir(str(hh.parent))
    assert s.ctx.access.readable_roots() == [str(hh.parent)]
