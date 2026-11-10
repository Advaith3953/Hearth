"""The access model through the tools: list anywhere for free, ask before reading, ask before changing."""
import os
from pathlib import Path

import pytest
from conftest import Asker

from hearth import tools_base
from hearth.access import Access
from hearth.index import Index
from hearth.journal import Journal
from hearth.tools import Ctx, run_tool
from hearth.tools_base import ALLOW_FILE, ALLOW_FOLDER, DENY


def run(ctx: Ctx, name: str, **args):
    return run_tool(ctx, name, args)


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    e = tmp_path / "elsewhere"
    (e / "sub").mkdir(parents=True)
    (e / "diary.txt").write_text("I saw a blue heron today.\n")
    (e / "other.txt").write_text("The key phrase is mauve elephant.\n")
    (e / "sub" / "deep.md").write_text("Deep note about mauve elephants.\n")
    (e / "pic.png").write_bytes(b"\x89PNG")
    (e / "song.mp3").write_bytes(b"ID3")
    return e


@pytest.fixture
def bare(hh: Path, asker: Asker) -> Ctx:
    """No trusted folder at all: Hearth must ask before reading anything."""
    box = Access(protected=[hh])
    return Ctx(box, Index(hh / "index.db", box), Journal(hh), asker, choose=asker.pick)


# ---------------------------------------------------------------- listing is free everywhere

def test_listing_anywhere_never_asks(ctx: Ctx, elsewhere: Path, asker: Asker):
    assert "diary.txt" in run(ctx, "list_folder", path=str(elsewhere))
    assert "sub/" in run(ctx, "list_folder", path=str(elsewhere))
    out = run(ctx, "folder_summary", path=str(elsewhere))
    assert "Total: 5 file(s)" in out and "Images: 1" in out and "Audio: 1" in out
    assert "pic.png" in run(ctx, "find_files", kind="image", folder=str(elsewhere))
    assert "file," in run(ctx, "file_info", path=str(elsewhere / "diary.txt"))
    assert asker.asked == []
    assert not ctx.access.read_free(str(elsewhere / "diary.txt"))                  # and nothing became readable by listing


def test_listing_hides_secret_stores(ctx: Ctx, tmp_path: Path):
    home = tmp_path / "fakehome"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("PRIVATE")
    (home / "readme.txt").write_text("hi")
    (home / "vault.kdbx").write_bytes(b"x")
    out = run(ctx, "list_folder", path=str(home))
    assert "readme.txt" in out and "vault.kdbx" not in out
    assert "never opens" in run(ctx, "list_folder", path=str(home / ".ssh"))
    assert "id_rsa" not in run(ctx, "find_files", pattern="id_*", folder=str(home))
    assert "vault" not in run(ctx, "folder_summary", path=str(home)).lower().replace("vault.kdbx", "")


# ---------------------------------------------------------------- reading asks

def test_reading_outside_asks_and_allow_file_covers_only_that_file(ctx: Ctx, elsewhere: Path, asker: Asker):
    asker.choice = ALLOW_FILE
    assert "blue heron" in run(ctx, "read_file", path=str(elsewhere / "diary.txt"))
    assert len(asker.asked) == 1 and asker.asked[0][0] == "Allow Hearth to read diary.txt?"
    assert "blue heron" in run(ctx, "read_file", path=str(elsewhere / "diary.txt"))      # remembered
    assert len(asker.asked) == 1
    run(ctx, "read_file", path=str(elsewhere / "other.txt"))                              # a different file asks again
    assert len(asker.asked) == 2


def test_allow_folder_covers_the_whole_folder_until_closed(ctx: Ctx, elsewhere: Path, asker: Asker):
    asker.choice = ALLOW_FOLDER
    assert "blue heron" in run(ctx, "read_file", path=str(elsewhere / "diary.txt"))
    assert "mauve" in run(ctx, "read_file", path=str(elsewhere / "other.txt"))
    assert "Deep note" in run(ctx, "read_file", path=str(elsewhere / "sub" / "deep.md"))
    assert len(asker.asked) == 1 and str(elsewhere) in ctx.access.session_dirs


def test_denied_read_reveals_nothing_and_tells_the_model_not_to_retry(ctx: Ctx, elsewhere: Path, asker: Asker):
    asker.choice = DENY
    out = run(ctx, "read_file", path=str(elsewhere / "diary.txt"))
    assert out.startswith("DENIED") and "blue heron" not in out and "Do not ask again" in out
    assert not ctx.access.read_free(str(elsewhere / "diary.txt"))


def test_sheet_tools_and_edit_tools_ask_before_reading_too(ctx: Ctx, elsewhere: Path, asker: Asker):
    asker.choice = DENY
    assert run(ctx, "sheet_info", path=str(elsewhere / "diary.txt")).startswith("DENIED")
    assert run(ctx, "replace_text", path=str(elsewhere / "diary.txt"), old="heron", new="owl").startswith("DENIED")
    assert run(ctx, "append_text", path=str(elsewhere / "diary.txt"), text="x").startswith("DENIED")
    assert run(ctx, "write_file", path=str(elsewhere / "diary.txt"), content="x", overwrite=True).startswith("DENIED")
    assert (elsewhere / "diary.txt").read_text() == "I saw a blue heron today.\n"


def test_editing_outside_needs_read_permission_and_then_approval_of_the_change(ctx: Ctx, elsewhere: Path, asker: Asker):
    asker.choice = ALLOW_FILE
    assert run(ctx, "replace_text", path=str(elsewhere / "diary.txt"), old="heron", new="owl").startswith("ERROR") is False
    assert "owl" in (elsewhere / "diary.txt").read_text()
    titles = [t for t, _ in asker.asked]
    assert titles[0].startswith("Allow Hearth to read") and titles[1].startswith("Change ")


def test_trusted_folders_read_without_asking(ctx: Ctx, root: Path, asker: Asker):
    assert "555-0142" in run(ctx, "read_file", path="notes.txt")
    assert asker.asked == []


def test_secret_stores_cannot_be_unlocked_by_saying_yes(ctx: Ctx, tmp_path: Path, asker: Asker):
    key = tmp_path / ".ssh" / "id_rsa"
    key.parent.mkdir()
    key.write_text("PRIVATE KEY")
    asker.choice = ALLOW_FOLDER
    assert "never opens" in run(ctx, "read_file", path=str(key))
    assert "PRIVATE" not in run(ctx, "search_text", query="PRIVATE", folder=str(key.parent))
    assert asker.asked == [] and not ctx.access.session_dirs


def test_directory_prompt_offers_folder_or_no(ctx: Ctx, elsewhere: Path, asker: Asker):
    seen: list[list[str]] = []
    ctx.choose = lambda t, d, opts: (seen.append(opts), len(opts) - 1)[1]
    run(ctx, "search_text", query="heron", folder=str(elsewhere))
    assert seen == [[ALLOW_FOLDER, DENY]]


def test_system_folders_read_with_permission_only(ctx: Ctx, tmp_path: Path, asker: Asker):
    sysdir = tmp_path / "winlike"
    sysdir.mkdir()
    (sysdir / "hosts.txt").write_text("127.0.0.1 localhost")
    ctx.access.system = [str(sysdir)]
    asker.choice = ALLOW_FILE
    assert "localhost" in run(ctx, "read_file", path=str(sysdir / "hosts.txt"))
    assert "never be changed" in asker.asked[0][1] or "never changed" in asker.asked[0][1]
    assert "operating-system" in run(ctx, "replace_text", path=str(sysdir / "hosts.txt"), old="127", new="128")
    assert "operating-system" in run(ctx, "delete_path", path=str(sysdir / "hosts.txt"))
    assert "operating-system" in run(ctx, "write_file", path=str(sysdir / "new.txt"), content="x")
    assert (sysdir / "hosts.txt").read_text() == "127.0.0.1 localhost"


# ---------------------------------------------------------------- search is permission-aware

def test_search_text_without_any_open_folder_explains_itself(bare: Ctx):
    out = run(bare, "search_text", query="anything")
    assert out.startswith("ERROR") and "folder=" in out


def test_search_text_in_another_folder_asks_then_indexes_and_finds(bare: Ctx, elsewhere: Path, asker: Asker):
    asker.choice = ALLOW_FOLDER
    out = run(bare, "search_text", query="mauve elephant", folder=str(elsewhere))
    assert str(elsewhere / "other.txt") in out and asker.asked and asker.asked[0][0] == "Allow Hearth to read elsewhere?"
    assert str(elsewhere / "sub" / "deep.md") not in out or "mauve" in out                  # AND-first ranking
    again = run(bare, "search_text", query="heron")                                          # now open for the session
    assert str(elsewhere / "diary.txt") in again and len(asker.asked) == 1


def test_search_text_denied_folder_finds_nothing(bare: Ctx, elsewhere: Path, asker: Asker):
    asker.choice = DENY
    assert run(bare, "search_text", query="heron", folder=str(elsewhere)).startswith("DENIED")
    assert not bare.access.readable_roots()


def test_search_text_rejects_files_as_folder_and_bad_kinds(ctx: Ctx, root: Path):
    assert "not a folder" in run(ctx, "search_text", query="x", folder=str(root / "notes.txt"))
    assert "kind must be" in run(ctx, "search_text", query="x", kind="banana")


def test_index_never_serves_files_that_stopped_being_readable(ctx: Ctx, elsewhere: Path, asker: Asker):
    asker.choice = ALLOW_FOLDER
    run(ctx, "search_text", query="heron", folder=str(elsewhere))
    assert ctx.index.search("heron")
    ctx.access.session_dirs.clear()                                                           # e.g. a new session
    assert ctx.index.search("heron") == []
    changed, gone = ctx.index.sync()
    assert gone == 0 and ctx.index.search("heron") == []                                      # rows outside scope are not served


def test_sync_prunes_only_inside_the_synced_roots_and_secret_rows(ctx: Ctx, elsewhere: Path, root: Path):
    ctx.index.sync([str(root)])
    ctx.index.sync([str(elsewhere)])
    (elsewhere / "diary.txt").unlink()
    changed, gone = ctx.index.sync([str(elsewhere)])
    assert gone == 1
    assert ctx.index.search("deposit")                                                         # the trusted rows survived
    ctx.index.sync([str(root)], seconds=-1)                                                    # out of time: nothing is dropped
    assert ctx.index.complete is False and ctx.index.search("deposit")


def test_index_skips_hidden_blocked_and_links(ctx: Ctx, elsewhere: Path):
    (elsewhere / ".hidden").mkdir()
    (elsewhere / ".hidden" / "h.txt").write_text("hiddenword")
    (elsewhere / "sub" / "loop").symlink_to(elsewhere, target_is_directory=True)
    ctx.access.grant_dir(str(elsewhere))
    ctx.index.sync([str(elsewhere)])
    assert ctx.index.search("hiddenword") == []
    assert len({h.path for h in ctx.index.search("mauve")}) == 2                               # not duplicated through the loop


def test_search_folder_filter(ctx: Ctx, elsewhere: Path, root: Path):
    ctx.access.grant_dir(str(elsewhere))
    ctx.index.sync()
    assert {h.path for h in ctx.index.search("mauve", folder=str(elsewhere / "sub"))} == {str(elsewhere / "sub" / "deep.md")}


# ---------------------------------------------------------------- folder_summary / find_files / list_folder

def test_folder_summary_counts_by_type_then_total(ctx: Ctx, root: Path):
    (root / "a.py").write_text("print(1)")
    (root / "b.csv").write_text("x,y\n1,2\n")
    (root / "photo.jpg").write_bytes(b"x")
    (root / "docs" / "c.xlsx").write_bytes(b"x")
    (root / "~$lock.docx").write_bytes(b"x")
    out = run(ctx, "folder_summary")
    assert "Text & data: 3" in out and "Code: 1" in out and "Spreadsheets: 2" in out and "Images: 1" in out
    assert out.rstrip().endswith("Total: 7 file(s)") and "1 sub-folder" in out
    assert out.index("Text & data") < out.index("Total")


def test_folder_summary_without_a_chosen_folder_lists_places(bare: Ctx):
    out = run(bare, "folder_summary")
    assert "No folder is chosen" in out and "Home:" in out and "ask the user" in out.lower()
    assert "Places on this computer" in run(bare, "list_folder")


def test_folder_summary_of_a_file_and_empty_folder(ctx: Ctx, root: Path):
    assert "not a folder" in run(ctx, "folder_summary", path=str(root / "notes.txt"))
    (root / "empty").mkdir()
    assert "(no files)" in run(ctx, "folder_summary", path=str(root / "empty")) and "Total: 0" in run(ctx, "folder_summary", path=str(root / "empty"))


def test_folder_summary_says_when_the_scan_was_cut_short(ctx: Ctx, monkeypatch: pytest.MonkeyPatch):
    import hearth.tools as tools
    monkeypatch.setattr(tools, "Budget", lambda seconds=0: tools_base.Budget(files=1))
    assert "only part of the folder" in run(ctx, "folder_summary")


def test_summary_and_find_skip_noisy_system_and_linked_folders(ctx: Ctx, root: Path, tmp_path: Path):
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x")
    (root / "AppData").mkdir()
    (root / "AppData" / "cache.dat").write_text("x")
    (root / ".git").mkdir()
    (root / ".git" / "cfg").write_text("x")
    (root / "loop").symlink_to(root, target_is_directory=True)
    out = run(ctx, "folder_summary")
    assert "Total: 3 file(s)" in out
    sysdir = tmp_path / "sysroot"
    (sysdir / "inner").mkdir(parents=True)
    (sysdir / "inner" / "a.txt").write_text("x")
    (root / "sys").symlink_to(sysdir, target_is_directory=True)                                # linked, so skipped
    assert "Total: 3 file(s)" in run(ctx, "folder_summary")
    ctx.access.system = [str(sysdir)]
    assert "Total: 1 file(s)" in run(ctx, "folder_summary", path=str(sysdir))                  # asked for directly: allowed


def test_find_files_by_name_and_kind_shows_dates_and_remembers(ctx: Ctx, root: Path):
    (root / "docs" / "budget.xlsx").write_bytes(b"x")
    out = run(ctx, "find_files", pattern="budget")
    assert "budget.xlsx" in out and "(1 B," in out and ctx.found == [str(root / "docs" / "budget.xlsx")]
    assert "docs/" in run(ctx, "find_files", pattern="docs")                                    # folders match by name too
    assert "todo.txt" in run(ctx, "find_files", pattern="*.txt", kind="text")
    assert "No matching" in run(ctx, "find_files", pattern="zzz")


def test_find_files_default_scope_is_trusted_folders_else_home(bare: Ctx, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake = tmp_path / "fakehome"
    fake.mkdir()
    (fake / "mine.txt").write_text("x")
    bare.access.home = str(fake)
    assert "mine.txt" in run(bare, "find_files", pattern="mine")


def test_list_folder_shows_unavailable_entries_and_truncates(ctx: Ctx, root: Path):
    big = root / "big"
    big.mkdir()
    for i in range(105):
        (big / f"f{i:03}.txt").write_text("x")
    out = run(ctx, "list_folder", path=str(big))
    assert "105 items" in out and "and 5 more" in out
    (root / "dangling").symlink_to(root / "gone")
    assert "dangling" in run(ctx, "list_folder", path=str(root))


# ---------------------------------------------------------------- changes keep asking

def test_auto_safe_only_applies_inside_trusted_folders(ctx: Ctx, root: Path, elsewhere: Path, asker: Asker):
    ctx.auto_approve_safe = True
    assert run(ctx, "write_file", path="fresh.txt", content="x").startswith("OK") and asker.asked == []
    assert run(ctx, "make_folder", path=str(root / "newdir")).startswith("OK") and asker.asked == []
    assert run(ctx, "write_file", path=str(elsewhere / "fresh.txt"), content="x").startswith("OK")
    assert len(asker.asked) == 1                                                               # outside: still asked
    assert run(ctx, "make_folder", path=str(elsewhere / "d")).startswith("OK") and len(asker.asked) == 2
    assert run(ctx, "copy_path", source=str(elsewhere / "fresh.txt"), destination=str(root / "copy.txt")).startswith("OK")
    assert len(asker.asked) == 3                                                               # one end outside: asked
    assert run(ctx, "move_path", source=str(root / "copy.txt"), destination=str(root / "docs")).startswith("OK")
    assert len(asker.asked) == 3                                                               # both ends trusted: not asked
    assert run(ctx, "move_path", source=str(root / "docs" / "copy.txt"), destination=str(elsewhere)).startswith("OK")
    assert len(asker.asked) == 4


def test_deleting_precious_places_is_refused_everywhere(ctx: Ctx, elsewhere: Path):
    assert "cannot delete" in run(ctx, "delete_path", path=ctx.access.home)
    assert "cannot move" in run(ctx, "move_path", source=ctx.access.home, destination=str(elsewhere / "sub"))
    assert "cannot delete" in run(ctx, "delete_path", path=str(ctx.access.trusted[0]))


def test_delete_outside_still_asks_and_is_undoable(ctx: Ctx, elsewhere: Path, asker: Asker):
    assert run(ctx, "delete_path", path=str(elsewhere / "pic.png")).startswith("OK")
    assert asker.asked[-1][0].startswith("Delete ") and not (elsewhere / "pic.png").exists()
    ctx.journal.undo()
    assert (elsewhere / "pic.png").exists()


def test_open_file_works_anywhere_but_always_asks(ctx: Ctx, elsewhere: Path, asker: Asker):
    assert run(ctx, "open_file", path=str(elsewhere / "pic.png")).startswith("OK") and asker.asked[-1][0].startswith("Open ")
    assert os.path.basename(ctx.opened[-1]) == "pic.png"                                       # type: ignore[attr-defined]


def test_pick_falls_back_to_plain_confirm_when_no_chooser(root: Path, hh: Path, asker: Asker):
    box = Access([root])
    c = Ctx(box, Index(hh / "i.db", box), Journal(hh), asker)
    assert c.pick("t", "d", ["a", "b", "c"]) == 0
    asker.answer = False
    assert c.pick("t", "d", ["a", "b", "c"]) == 2


def test_standard_folder_names_resolve_from_home_and_limit_shows_newest(ctx: Ctx, tmp_path: Path):
    home = tmp_path / "fakehome"
    (home / "Downloads").mkdir(parents=True)
    for i in range(8):
        f = home / "Downloads" / f"doc{i}.pdf"
        f.write_bytes(b"x")
        os.utime(f, (1_000_000 + i * 100, 1_000_000 + i * 100))
    ctx.access.home = str(home)
    assert "doc7.pdf" in run(ctx, "list_folder", path="Downloads")
    out = run(ctx, "find_files", kind="pdf", folder="Downloads", limit=5)
    assert out.startswith("The 5 newest of 8 matches") and out.index("doc7.pdf") < out.index("doc3.pdf") and "doc2.pdf" not in out
    assert ctx.found[0].endswith("doc7.pdf") and len(ctx.found) == 5
    assert run(ctx, "find_files", pattern="doc3", folder="Downloads").count("doc") == 1
    assert "file, not a folder" in run(ctx, "list_folder", path="Downloads/doc1.pdf")


def test_progress_line_names_the_folder():
    from hearth.agent import describe
    assert describe("find_files", {"kind": "pdf", "folder": "C:\\Users\\a\\Downloads", "limit": 5}) == "Looking for pdf files in Downloads"
