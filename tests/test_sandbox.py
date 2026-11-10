import os
from pathlib import Path

import pytest

from hearth.sandbox import Sandbox, SandboxError


def test_inside_paths_resolve(root: Path):
    box = Sandbox([root])
    assert box.resolve(str(root / "notes.txt")) == (root / "notes.txt").resolve()
    assert box.resolve("notes.txt") == (root / "notes.txt").resolve()      # relative = inside the first shared folder
    assert box.resolve("docs/../notes.txt") == (root / "notes.txt").resolve()


@pytest.mark.parametrize("bad", ["../outside.txt", "docs/../../outside.txt", "/etc/passwd", "~/.ssh/id_rsa", "", "   ", "a\0b"])
def test_escapes_are_refused(root: Path, bad: str):
    with pytest.raises(SandboxError):
        Sandbox([root]).resolve(bad, must_exist=False)


def test_sibling_folder_with_same_prefix_is_outside(root: Path, tmp_path: Path):
    evil = tmp_path / "shared-evil"
    evil.mkdir()
    (evil / "x.txt").write_text("x")
    with pytest.raises(SandboxError):
        Sandbox([root]).resolve(str(evil / "x.txt"))


def test_symlink_out_of_the_folder_is_refused(root: Path, tmp_path: Path):
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    try:
        os.symlink(secret, root / "link.txt")
        os.symlink(tmp_path, root / "dirlink")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    box = Sandbox([root])
    for p in ("link.txt", "dirlink", "dirlink/secret.txt"):
        with pytest.raises(SandboxError):
            box.resolve(p)


def test_symlink_inside_the_folder_is_still_not_followed(root: Path):
    try:
        os.symlink(root / "notes.txt", root / "alias.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    with pytest.raises(SandboxError, match="link"):
        Sandbox([root]).resolve("alias.txt")


def test_protected_folder_is_unreachable_even_inside_a_shared_folder(root: Path):
    data = root / "hearth-data"
    data.mkdir()
    (data / "config.json").write_text("{}")
    box = Sandbox([root], protected=[data])
    with pytest.raises(SandboxError):
        box.resolve(str(data / "config.json"))
    assert not box.contains(str(data.resolve()))


def test_missing_and_no_roots(root: Path):
    with pytest.raises(SandboxError, match="does not exist"):
        Sandbox([root]).resolve("nope.txt")
    assert Sandbox([root]).resolve("nope.txt", must_exist=False).name == "nope.txt"
    with pytest.raises(SandboxError, match="no folders"):
        Sandbox([]).resolve("a.txt")


def test_relative_path_finds_the_right_shared_folder(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (b / "only-in-b.txt").write_text("x")
    box = Sandbox([a, b])
    assert box.resolve("only-in-b.txt").parent == b.resolve()
    assert box.resolve("new.txt", must_exist=False).parent == a.resolve()


def test_is_root(root: Path):
    box = Sandbox([root])
    assert box.is_root(str(root.resolve()))
    assert not box.is_root(str((root / "docs").resolve()))
