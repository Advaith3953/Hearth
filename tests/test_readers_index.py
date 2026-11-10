import contextlib
import zipfile
from pathlib import Path

import pytest

from hearth import readers
from hearth.access import Access
from hearth.index import Index, chunks


def make_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')


def test_docx_text_is_extracted(tmp_path: Path):
    p = tmp_path / "a.docx"
    make_docx(p, ["Hello &amp; welcome", "Second line"])
    assert readers.read_text(str(p)) == "Hello & welcome\nSecond line"


def test_bad_docx_and_binary_and_large(tmp_path: Path):
    bad = tmp_path / "x.docx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(readers.UnreadableError):
        readers.read_text(str(bad))
    b = tmp_path / "pic.png"
    b.write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0")
    with pytest.raises(readers.UnreadableError, match="binary"):
        readers.read_text(str(b))
    big = tmp_path / "big.txt"
    big.write_text("x" * 50)
    with pytest.raises(readers.UnreadableError, match="too large"):
        readers.read_text(str(big), limit=10)


def test_utf16_and_strict(tmp_path: Path):
    p = tmp_path / "u.txt"
    p.write_bytes("héllo".encode("utf-16"))
    assert readers.read_text(str(p)) == "héllo"
    with pytest.raises(readers.UnreadableError):
        readers.read_text_strict(str(p))
    latin = tmp_path / "l.txt"
    latin.write_bytes("café".encode("latin-1"))
    with pytest.raises(readers.UnreadableError):
        readers.read_text_strict(str(latin))
    assert "caf" in readers.read_text(str(latin))      # lenient read still works


def test_editable_rules():
    assert readers.editable("a.txt") and readers.editable("Makefile") and not readers.editable("a.docx")
    assert not readers.editable("a.png")


def test_chunks_cover_text_and_split_long_paragraphs():
    text = "para one\n\n" + ("word " * 600) + "\n\nlast"
    parts = chunks(text, 500)
    assert all(len(p) <= 500 for p in parts)
    assert "last" in parts[-1] and "para one" in parts[0]
    assert chunks("") == []


def test_index_sync_search_update_and_remove(root: Path, hh: Path):
    box = Access([root], protected=[hh])
    idx = Index(hh / "i.db", box)
    changed, gone = idx.sync()
    assert changed == 3 and gone == 0
    assert idx.sync() == (0, 0)                                   # nothing changed: nothing redone
    hit = idx.search("deposit")
    assert hit and hit[0].path.endswith("lease.md") and "[deposit]" in hit[0].snippet
    (root / "docs" / "lease.md").write_text("Landlord is Mr Rao.", encoding="utf-8")
    idx.sync()
    assert not idx.search("deposit") and idx.search("landlord")
    (root / "notes.txt").unlink()
    assert idx.sync()[1] == 1 and not idx.search("rent")


@pytest.mark.parametrize("q", ['"', "AND", "NEAR(a b)", "a*", "'; DROP TABLE files;--", "", "   ", "!!!", "x" * 5000])
def test_search_never_crashes_on_odd_queries(root: Path, hh: Path, q: str):
    box = Access([root], protected=[hh])
    idx = Index(hh / "i.db", box)
    idx.sync()
    assert isinstance(idx.search(q), list)


def test_index_skips_hidden_protected_links_and_binaries(root: Path, hh: Path, tmp_path: Path):
    (root / ".hidden").mkdir()
    (root / ".hidden" / "s.txt").write_text("hiddenword")
    (root / "img.png").write_bytes(b"\0\0\0 pngword")
    outside = tmp_path / "out.txt"
    outside.write_text("outsideword")
    with contextlib.suppress(OSError, NotImplementedError):
        (root / "l.txt").symlink_to(outside)
    inner = root / "hearthdata"
    inner.mkdir()
    (inner / "journal.txt").write_text("protectedword")
    idx = Index(hh / "i.db", Access([root], protected=[inner]))
    idx.sync()
    for w in ("hiddenword", "pngword", "outsideword", "protectedword"):
        assert idx.search(w) == [], w


def test_refresh_and_forget(root: Path, hh: Path):
    idx = Index(hh / "i.db", Access([root], protected=[hh]))
    idx.sync()
    new = root / "new.txt"
    new.write_text("zebra crossing")
    idx.refresh(str(new))
    assert idx.search("zebra")
    idx.forget(str(root / "docs"))                                 # a folder: everything below it goes
    assert not idx.search("deposit") and idx.search("zebra")
    new.unlink()
    idx.refresh(str(new))
    assert not idx.search("zebra")


def test_search_drops_hits_from_folders_no_longer_shared(root: Path, hh: Path, tmp_path: Path):
    idx = Index(hh / "i.db", Access([root], protected=[hh]))
    idx.sync()
    other = tmp_path / "other"
    other.mkdir()
    narrowed = Index(hh / "i.db", Access([other], protected=[hh]))
    assert narrowed.search("deposit") == []


def test_search_prefers_files_with_all_the_words(ctx):
    root = ctx.access.trusted[0]
    from pathlib import Path
    (Path(root) / "a.txt").write_text("invoice 4470 paid")
    (Path(root) / "b.txt").write_text("invoice 4471 is overdue")
    (Path(root) / "c.txt").write_text("invoice 4472 pending")
    ctx.index.sync()
    assert [Path(h.path).name for h in ctx.index.search("invoice 4471")] == ["b.txt"]
    got = {Path(h.path).name for h in ctx.index.search("invoice nonexistentword")}     # none has both -> any word
    assert got == {"a.txt", "b.txt", "c.txt"}
    assert {Path(h.path).name for h in ctx.index.search("invoice")} == {"a.txt", "b.txt", "c.txt"}
