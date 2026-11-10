import os
from pathlib import Path

import pytest
from conftest import Asker

from hearth.journal import Journal, UndoError
from hearth.tools import TOOLS, Ctx, run_tool


def run(ctx: Ctx, name: str, **args):
    return run_tool(ctx, name, args)


def test_read_only_tools(ctx: Ctx, root: Path, asker: Asker):
    assert "notes.txt" in run(ctx, "list_folder", path=str(root)) and "docs/" in run(ctx, "list_folder", path=str(root))
    assert str(ctx.access.trusted[0]) in run(ctx, "list_folder")
    assert "lease.md" in run(ctx, "find_files", pattern="lease")
    assert "lease.md" in run(ctx, "find_files", pattern="*.md", folder="docs")
    assert "No matching files" in run(ctx, "find_files", pattern="zzz")
    ctx.index.sync()
    assert "deposit" in run(ctx, "search_text", query="deposit")
    assert "No matches" in run(ctx, "search_text", query="unicorn")
    assert "555-0142" in run(ctx, "read_file", path="notes.txt")
    assert "file," in run(ctx, "file_info", path="notes.txt") and "folder," in run(ctx, "file_info", path="docs")
    assert asker.asked == []                                          # reading never asks


def test_read_file_pages_through_long_text(ctx: Ctx, root: Path):
    (root / "long.txt").write_text("a" * 12000)
    first = run(ctx, "read_file", path="long.txt")
    assert "offset=5000" in first
    assert run(ctx, "read_file", path="long.txt", offset=10000).count("a") == 2000
    assert run(ctx, "read_file", path="long.txt", offset=99999) == "(end of file)"
    (root / "empty.txt").write_text("")
    assert "empty" in run(ctx, "read_file", path="empty.txt")


def test_errors_are_text_not_exceptions(ctx: Ctx, root: Path):
    assert run(ctx, "read_file", path="/nonexistent/x.txt").startswith("ERROR: '/nonexistent/x.txt' does not exist")
    assert "never opens it" in run(ctx, "read_file", path="~/.ssh/id_rsa")
    assert "folder" in run(ctx, "read_file", path="docs")
    assert "file, not a folder" in run(ctx, "list_folder", path="notes.txt")
    (root / "b.bin").write_bytes(b"\0\1\2")
    assert "binary" in run(ctx, "read_file", path="b.bin")
    assert "no tool called" in run_tool(ctx, "format_disk", {})
    assert "unknown argument" in run(ctx, "read_file", path="notes.txt", mode="x")
    assert "missing required" in run_tool(ctx, "read_file", {})
    assert "must be" in run_tool(ctx, "read_file", {"path": ["a"]})


def test_arguments_from_small_models_are_coerced(ctx: Ctx, root: Path):
    (root / "n.txt").write_text("x" * 6000)
    assert "x" in run_tool(ctx, "read_file", {"path": "n.txt", "offset": "5000"})
    assert "OK" in run_tool(ctx, "write_file", {"path": "t.txt", "content": 12345})
    assert (root / "t.txt").read_text() == "12345"
    assert "OK" in run_tool(ctx, "write_file", {"path": "t.txt", "content": "z", "overwrite": "true"})
    assert "must be" in run_tool(ctx, "read_file", {"path": "n.txt", "offset": True})


def test_write_new_file_asks_and_is_undoable(ctx: Ctx, root: Path, asker: Asker):
    out = run(ctx, "write_file", path="new/plan.md", content="# Plan\n- one\n")
    assert out.startswith("OK") and (root / "new" / "plan.md").read_text() == "# Plan\n- one\n"
    assert asker.asked and "Create" in asker.asked[0][0]
    assert "plan" in run(ctx, "search_text", query="plan")             # index refreshed immediately
    assert "Removed" in ctx.journal.undo()
    assert not (root / "new" / "plan.md").exists()


def test_write_refuses_existing_unless_overwrite(ctx: Ctx, root: Path):
    out = run(ctx, "write_file", path="notes.txt", content="x")
    assert out.startswith("ERROR") and "replace_text" in out
    assert (root / "notes.txt").read_text().startswith("Rent")
    assert run(ctx, "write_file", path="notes.txt", content="fresh", overwrite=True).startswith("OK")
    assert (root / "notes.txt").read_text() == "fresh"
    assert "Restored" in ctx.journal.undo()
    assert (root / "notes.txt").read_text().startswith("Rent")


def test_denied_changes_leave_everything_untouched(ctx: Ctx, root: Path, asker: Asker):
    asker.answer = False
    before = {p: p.read_text() for p in root.rglob("*.*")}
    calls = [("write_file", {"path": "a.txt", "content": "x"}), ("write_file", {"path": "notes.txt", "content": "x", "overwrite": True}),
             ("replace_text", {"path": "notes.txt", "old": "Rent", "new": "Tax"}), ("append_text", {"path": "notes.txt", "text": "z"}),
             ("make_folder", {"path": "nf"}), ("move_path", {"source": "notes.txt", "destination": "docs"}),
             ("copy_path", {"source": "notes.txt", "destination": "docs"}), ("delete_path", {"path": "notes.txt"}),
             ("open_file", {"path": "notes.txt"})]
    for name, args in calls:
        assert run_tool(ctx, name, args).startswith("DENIED"), name
    assert {p: p.read_text() for p in root.rglob("*.*")} == before
    assert not (root / "nf").exists() and ctx.journal.recent() == []


def test_replace_text(ctx: Ctx, root: Path, asker: Asker):
    assert run(ctx, "replace_text", path="notes.txt", old="1st", new="5th").startswith("OK")
    assert "5th" in (root / "notes.txt").read_text()
    assert "before: notes.txt" in asker.asked[-1][1] and "-Rent is due on the 1st." in asker.asked[-1][1]
    assert "not found" in run(ctx, "replace_text", path="notes.txt", old="zzz", new="y")
    assert "must not be empty" in run(ctx, "replace_text", path="notes.txt", old="", new="y")
    (root / "dup.txt").write_text("a\nb\na\n")
    assert "2 times" in run(ctx, "replace_text", path="dup.txt", old="a", new="c")
    assert run(ctx, "replace_text", path="dup.txt", old="a", new="c", replace_all=True).startswith("OK: replaced 2")
    assert (root / "dup.txt").read_text() == "c\nb\nc\n"
    ctx.journal.undo()
    assert (root / "dup.txt").read_text() == "a\nb\na\n"


def test_edits_keep_windows_line_endings(ctx: Ctx, root: Path):
    (root / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    assert run(ctx, "replace_text", path="w.txt", old="one\ntwo", new="ONE\nTWO").startswith("OK")
    assert (root / "w.txt").read_bytes() == b"ONE\r\nTWO\r\nthree\r\n"
    run(ctx, "append_text", path="w.txt", text="four")
    assert (root / "w.txt").read_bytes() == b"ONE\r\nTWO\r\nthree\r\nfour"


def test_append_text(ctx: Ctx, root: Path):
    run(ctx, "append_text", path="docs/todo.txt", text="buy bread")
    assert (root / "docs" / "todo.txt").read_text() == "buy milk\nbuy eggs\nbuy bread"
    assert run(ctx, "append_text", path="fresh.txt", text="hi").startswith("OK")
    assert (root / "fresh.txt").read_text() == "hi"
    assert "Removed" in ctx.journal.undo()                            # undoing the creation
    assert not (root / "fresh.txt").exists()


def test_only_plain_text_can_be_modified(ctx: Ctx, root: Path):
    (root / "r.docx").write_bytes(b"PK")
    (root / "p.pdf").write_bytes(b"%PDF")
    for name, args in (("write_file", {"path": "r.docx", "content": "x", "overwrite": True}),
                       ("replace_text", {"path": "p.pdf", "old": "a", "new": "b"}), ("append_text", {"path": "p.pdf", "text": "x"})):
        assert "plain-text" in run_tool(ctx, name, args)
    assert "not a readable .docx" in run_tool(ctx, "replace_text", {"path": "r.docx", "old": "a", "new": "b"})
    (root / "latin.txt").write_bytes("café".encode("latin-1"))
    assert "UTF-8" in run(ctx, "replace_text", path="latin.txt", old="caf", new="x")


def test_move_copy_folder_and_undo(ctx: Ctx, root: Path):
    assert run(ctx, "make_folder", path="Archive").startswith("OK")
    assert "already exists" in run(ctx, "make_folder", path="Archive")
    assert run(ctx, "move_path", source="docs/todo.txt", destination="Archive").startswith("OK")
    assert (root / "Archive" / "todo.txt").exists() and not (root / "docs" / "todo.txt").exists()
    assert "eggs" in run(ctx, "search_text", query="eggs")                # index follows the move
    assert run(ctx, "copy_path", source="docs", destination="Archive/docs-copy").startswith("OK")
    assert (root / "Archive" / "docs-copy" / "lease.md").exists() and (root / "docs" / "lease.md").exists()
    assert run(ctx, "move_path", source="notes.txt", destination="notes2.txt").startswith("OK")
    assert "already exists" in run(ctx, "copy_path", source="docs/lease.md", destination="Archive/docs-copy/lease.md")
    ctx.journal.undo(), ctx.journal.undo()
    assert (root / "notes.txt").exists() and not (root / "notes2.txt").exists()
    assert not (root / "Archive" / "docs-copy").exists() and (root / "Archive" / "todo.txt").exists()
    ctx.journal.undo()
    assert (root / "docs" / "todo.txt").exists()


def test_cannot_move_into_itself_or_touch_the_shared_folder_itself(ctx: Ctx, root: Path):
    assert "into itself" in run(ctx, "move_path", source="docs", destination="docs/inner")
    assert "trusted folder itself" in run(ctx, "move_path", source=str(root), destination="x")
    assert "trusted folder itself" in run(ctx, "delete_path", path=str(root))
    assert root.exists()


def test_delete_goes_to_trash_and_can_be_restored(ctx: Ctx, root: Path, asker: Asker):
    ctx.index.sync()
    out = run(ctx, "delete_path", path="docs")
    assert out.startswith("OK") and not (root / "docs").exists()
    assert "2 file(s)" in asker.asked[-1][1]
    assert "No matches" in run(ctx, "search_text", query="deposit")
    assert any(ctx.journal.trash.rglob("lease.md"))                    # recoverable, not destroyed
    assert "Restored" in ctx.journal.undo()
    assert (root / "docs" / "lease.md").read_text().startswith("# Lease")


def test_open_file_is_allowlisted_and_asks(ctx: Ctx, root: Path, asker: Asker):
    (root / "r.pdf").write_bytes(b"%PDF")
    (root / "run.exe").write_bytes(b"MZ")
    (root / "go.bat").write_text("echo hi")
    assert run(ctx, "open_file", path="r.pdf").startswith("OK") and ctx.opened[0].endswith("r.pdf")  # type: ignore[attr-defined]
    for bad in ("run.exe", "go.bat"):
        assert run(ctx, "open_file", path=bad).startswith("ERROR")
    assert len(ctx.opened) == 1                                       # type: ignore[attr-defined]
    assert asker.asked                                                # always asks


def test_open_failure_is_reported(ctx: Ctx, root: Path):
    (root / "r.pdf").write_bytes(b"%PDF")

    def boom(_: str) -> None:
        raise OSError("no app")
    ctx.opener = boom
    assert "could not open" in run(ctx, "open_file", path="r.pdf")


def test_auto_approve_safe_skips_only_harmless_questions(ctx: Ctx, root: Path, asker: Asker):
    ctx.auto_approve_safe = True
    run(ctx, "write_file", path="a.txt", content="x")
    run(ctx, "make_folder", path="d")
    run(ctx, "move_path", source="a.txt", destination="d")
    assert asker.asked == []
    run(ctx, "delete_path", path="d")
    run(ctx, "replace_text", path="notes.txt", old="Rent", new="Tax")
    run(ctx, "write_file", path="notes.txt", content="y", overwrite=True)
    assert len(asker.asked) == 3                                      # delete, edit, overwrite still ask


def test_os_errors_become_messages(ctx: Ctx, root: Path, monkeypatch: pytest.MonkeyPatch):
    def refuse(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr("hearth.tools.shutil.move", refuse)
    assert "operating system refused" in run(ctx, "delete_path", path="notes.txt")


def test_big_outputs_are_cut(ctx: Ctx, root: Path):
    for i in range(120):
        (root / f"f{i:03}.txt").write_text("x")
    out = run(ctx, "list_folder", path=str(root))
    assert "and 22 more" in out
    out = run(ctx, "find_files", pattern="f*.txt")
    assert "The 40 newest of 120" in out


def test_every_tool_has_a_signature_and_doc():
    assert len(TOOLS) == 35
    for t in TOOLS.values():
        assert t.name in t.signature() and t.doc


def test_journal_behaviour(hh: Path, tmp_path: Path):
    j = Journal(hh)
    with pytest.raises(UndoError, match="nothing"):
        j.undo()
    f = tmp_path / "f.txt"
    f.write_text("v1")
    b = j.backup(str(f))
    j.record("edit", path=str(f), backup=b)
    f.write_text("v2")
    assert "Restored" in j.undo() and f.read_text() == "v1"
    with pytest.raises(UndoError):
        j.undo()                                                       # already undone
    j.record("move", src=str(tmp_path / "a"), dst=str(tmp_path / "gone"))
    with pytest.raises(UndoError, match="no longer"):
        j.undo()
    f.unlink()
    j.record("delete", path=str(f), trashed=str(tmp_path / "missing"))
    with pytest.raises(UndoError, match="no longer in the trash"):
        j.undo()
    f.write_text("back")
    j.record("move", src=str(f), dst=str(tmp_path / "x"))     # conflict (src exists) is retryable, not skipped
    (tmp_path / "x").write_text("x")
    with pytest.raises(UndoError, match="something else"):
        j.undo()
    with pytest.raises(UndoError, match="something else"):
        j.undo()
    f.unlink()
    assert "Moved" in j.undo()
    j.record("bogus")
    with pytest.raises(UndoError, match="unknown"):
        j.undo()
    assert len(j.recent(3)) == 3


def test_purge_removes_only_old_items(hh: Path, tmp_path: Path):
    j = Journal(hh)
    slot = j.trash_slot()
    (slot / "old.txt").write_text("x")
    fresh = j.trash_slot()
    (fresh / "new.txt").write_text("y")
    src = tmp_path / "s.txt"
    src.write_text("z")
    old_backup = Path(j.backup(str(src)))
    long_ago = 1_000_000_000
    for p in (slot, old_backup):
        os.utime(p, (long_ago, long_ago))
    assert j.purge(30) == 2
    assert fresh.exists() and not slot.exists() and not old_backup.exists()
    assert Journal(hh / "nowhere").purge() == 0
