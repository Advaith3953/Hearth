import os
from pathlib import Path

import pytest

from hearth import app as hearth_app
from hearth.access import Access, AccessError, dedupe, drives, within
from hearth.filetypes import kind_of, normalize_kind
from hearth.tools_base import Budget, is_link


def test_paths_resolve_anywhere_not_only_in_trusted_folders(root: Path, tmp_path: Path):
    box = Access([root])
    other = tmp_path / "elsewhere" / "x.txt"
    other.parent.mkdir()
    other.write_text("x")
    assert box.resolve(str(other)) == other.resolve()                          # listing/looking is allowed everywhere
    assert box.resolve("notes.txt") == (root / "notes.txt").resolve()          # relative = first trusted folder that has it
    assert box.resolve("docs/../notes.txt") == (root / "notes.txt").resolve()
    assert box.resolve("new.txt", must_exist=False) == (root / "new.txt").resolve()


def test_relative_paths_fall_back_to_home_without_trusted_folders():
    box = Access()
    assert box.resolve("anything-not-there.txt", must_exist=False) == Path(box.home, "anything-not-there.txt").resolve()


@pytest.mark.parametrize("bad", ["", "   ", "a\0b"])
def test_invalid_paths_are_refused(bad: str):
    with pytest.raises(AccessError):
        Access().resolve(bad, must_exist=False)


@pytest.mark.parametrize("secret", ["~/.ssh/id_rsa", "/home/u/.aws/credentials", "C:/Users/u/AppData/Local/Google/Chrome/User Data/Default/Login Data",
                                    "/x/vault.kdbx", "/home/u/.config/google-chrome/Default", "/x/.git-credentials", "/x/key.pfx"])
def test_secret_stores_are_never_opened(secret: str):
    box = Access()
    assert box.is_blocked(os.path.expanduser(secret))
    with pytest.raises(AccessError, match="never opens"):
        box.resolve(secret, must_exist=False)


def test_blocked_beats_trusted(tmp_path: Path):
    ssh = tmp_path / "home" / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "id_rsa").write_text("PRIVATE")
    box = Access([tmp_path / "home"])
    assert not box.read_free(str(ssh / "id_rsa"))
    with pytest.raises(AccessError):
        box.resolve(str(ssh / "id_rsa"))


def test_hearths_own_folder_is_never_opened(hh: Path):
    hh.mkdir(parents=True)
    (hh / "config.json").write_text("{}")
    with pytest.raises(AccessError, match="own data"):
        Access(protected=[hh]).resolve(str(hh / "config.json"))


def test_links_are_not_followed(root: Path, tmp_path: Path):
    link = root / "link.txt"
    try:
        link.symlink_to(tmp_path)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert is_link(str(link))
    with pytest.raises(AccessError, match="link"):
        Access([root]).resolve(str(link))


def test_read_free_only_for_trusted_and_session_grants(root: Path, tmp_path: Path):
    box = Access([root])
    inside, outside = str(root / "notes.txt"), tmp_path / "o" / "a.txt"
    outside.parent.mkdir()
    outside.write_text("a")
    sibling = outside.parent / "b.txt"
    sibling.write_text("b")
    assert box.read_free(inside) and not box.read_free(str(outside))
    box.grant_file(str(outside))
    assert box.read_free(str(outside)) and not box.read_free(str(sibling))
    box.grant_dir(str(outside.parent))
    box.grant_dir(str(outside.parent))                                          # idempotent
    assert box.read_free(str(sibling)) and box.session_dirs == [str(outside.parent)]
    assert set(box.readable_roots()) == {str(root.resolve()), str(outside.parent)}


def test_system_folders_can_be_read_with_permission_but_never_changed(tmp_path: Path):
    sysdir = tmp_path / "winlike"
    sysdir.mkdir()
    (sysdir / "f.txt").write_text("f")
    box = Access()
    box.system = [str(sysdir)]
    assert box.is_system(str(sysdir / "f.txt"))
    with pytest.raises(AccessError, match="operating-system"):
        box.check_writable(str(sysdir / "f.txt"))
    assert box.is_precious(str(sysdir))
    box.check_writable(str(tmp_path / "ordinary.txt"))                          # elsewhere is fine


def test_precious_places_cannot_be_deleted_or_moved(root: Path):
    box = Access([root])
    assert box.is_precious(box.home) and box.is_precious(str(root)) and box.is_precious(os.path.abspath(os.sep))
    assert box.is_precious(os.path.join(box.home, "Documents"))
    assert not box.is_precious(str(root / "docs"))


def test_places_start_points(root: Path):
    places = Access([root, root]).places()
    labels = [lbl for lbl, _ in places]
    assert labels[0] == "Home" and "Trusted folder" in labels and "Drive" in labels
    assert len({p for _, p in places}) == len(places)                           # no duplicates
    assert drives()


def test_within_and_dedupe(tmp_path: Path):
    a, b = str(tmp_path / "a"), str(tmp_path / "a" / "b")
    assert within(b, a) and not within(a, b) and not within(str(tmp_path / "ab"), a)
    assert dedupe([b, a, a]) == [a]


def test_budget_stops_big_scans():
    b = Budget(files=3)
    assert not b.spent(2) and b.spent(2) and b.truncated
    assert Budget(seconds=-1).spent()


def test_kinds_cover_everyday_files():
    cases = {"a.xlsx": "excel", "a.CSV": "excel", "a.docx": "word", "a.pdf": "pdf", "a.pptx": "presentation", "a.png": "image",
             "a.mp3": "audio", "a.mp4": "video", "a.zip": "archive", "a.exe": "program", "a.py": "code", "a.md": "text",
             "README": "text", "a.weird": "other"}
    for name, want in cases.items():
        assert kind_of(name) == want, name
    for raw, want in (("Excel files", "excel"), ("photos", "image"), ("my Songs", "audio"), ("mp4", "video"), ("zip", "archive"),
                      ("PowerPoint", "presentation"), ("all", ""), ("scripts", "code")):
        assert normalize_kind(raw) == want, raw
    assert normalize_kind("banana") is None


def test_check_grantable_refuses_risky_folders(tmp_path: Path, hh: Path, monkeypatch: pytest.MonkeyPatch):
    ok = tmp_path / "Notes"
    ok.mkdir()
    assert hearth_app.check_grantable(str(ok)) == str(ok.resolve())
    with pytest.raises(ValueError, match="not a folder"):
        hearth_app.check_grantable(str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="entire drive"):
        hearth_app.check_grantable(os.path.abspath(os.sep))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    with pytest.raises(ValueError, match="passwords"):
        hearth_app.check_grantable(str(ssh))
    hh.mkdir(parents=True)
    with pytest.raises(ValueError, match="own data"):
        hearth_app.check_grantable(str(hh))
    monkeypatch.setattr("hearth.app.Access", lambda: type("A", (), {"is_blocked": lambda s, p: False, "is_system": lambda s, p: True})())
    with pytest.raises(ValueError, match="operating-system"):
        hearth_app.check_grantable(str(ok))
