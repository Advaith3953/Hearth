"""The fifteen newer tools: compare, duplicates, tidy, rename, PDFs, templates, new spreadsheets, zips, history/undo, deadlines."""
import datetime as dt
import json
import os
import time
import zipfile
from pathlib import Path

import pytest
from conftest import Asker
from docx import Document
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from hearth import docxio, sheets, tools_more
from hearth.agent import Agent, wants_change
from hearth.model import ScriptedModel
from hearth.tools import TOOLS, Ctx, run_tool


def run(ctx: Ctx, tool: str, /, **a) -> str:
    return run_tool(ctx, tool, a)


def put(p: Path, text: str = "x", age_days: float = 0.0) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
    return p


def make_pdf(path: Path, pages: int = 2, label: str = "page") -> Path:
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(72, 700, f"{label} {i + 1} text")
        c.showPage()
    c.save()
    return path


@pytest.fixture
def box(root: Path) -> Path:
    d = root / "box"
    d.mkdir()
    return d


# ---------------------------------------------------------------- compare

def test_compare_text_files_shows_the_changed_lines(ctx: Ctx, box: Path):
    a, b = put(box / "a.txt", "one\ntwo\nthree\n"), put(box / "b.txt", "one\n2\nthree\nfour\n")
    out = run(ctx, "compare_files", first=str(a), second=str(b))
    assert "1 line(s) only in a.txt" in out and "2 line(s) only in b.txt" in out and "-two" in out and "+2" in out and "+four" in out
    assert "identical" in run(ctx, "compare_files", first=str(a), second=str(a))


def test_compare_spreadsheets_by_cell(ctx: Ctx, box: Path):
    sheets.create_xlsx(str(box / "v1.xlsx"), [("Pay", [["Name", "Amt"], ["Asha", "10"], ["Ravi", "20"]])])
    sheets.create_xlsx(str(box / "v2.xlsx"), [("Pay", [["Name", "Amt"], ["Asha", "10"], ["Ravi", "25"]]), ("New", [["a"]])])
    out = run(ctx, "compare_files", first=str(box / "v1.xlsx"), second=str(box / "v2.xlsx"))
    assert "Pay!B3: '20' -> '25'" in out and "sheet 'New' only exists in v2.xlsx" in out
    assert "same values" in run(ctx, "compare_files", first=str(box / "v1.xlsx"), second=str(box / "v1.xlsx"))


def test_compare_word_pdf_and_binary_files(ctx: Ctx, box: Path):
    docxio.create(str(box / "a.docx"), ["Hello", "World"])
    docxio.create(str(box / "b.docx"), ["Hello", "There"])
    assert "-World" in run(ctx, "compare_files", first=str(box / "a.docx"), second=str(box / "b.docx"))
    (box / "x.bin").write_bytes(b"\0\1\2" * 10)
    (box / "y.bin").write_bytes(b"\0\1\2" * 10)
    (box / "z.bin").write_bytes(b"\0\1\3" * 10)
    assert "identical, byte for byte" in run(ctx, "compare_files", first=str(box / "x.bin"), second=str(box / "y.bin"))
    assert "not text" in run(ctx, "compare_files", first=str(box / "x.bin"), second=str(box / "z.bin"))
    assert run(ctx, "compare_files", first=str(box), second=str(box / "x.bin")).startswith("ERROR")


# ---------------------------------------------------------------- duplicates

def test_duplicates_are_found_and_moved_with_one_undo(ctx: Ctx, box: Path, asker: Asker):
    old = put(box / "report.txt", "same content here", age_days=9)
    put(box / "copy of report.txt", "same content here", age_days=3)
    put(box / "sub" / "report again.txt", "same content here", age_days=1)
    put(box / "other.txt", "different")
    put(box / "tiny.txt", "")
    out = run(ctx, "find_duplicates", folder=str(box))
    assert "1 group(s)" in out and "3 identical copies" in out and "copy of report.txt" in out and "other.txt" not in out
    assert run(ctx, "find_duplicates", folder=str(box), min_kb=5).startswith("No duplicate")
    done = run(ctx, "move_duplicates", folder=str(box))
    assert done.startswith("OK: moved 2") and old.exists() and not (box / "copy of report.txt").exists()
    assert sorted(p.name for p in (box / "_duplicates").iterdir()) == ["copy of report.txt", "report again.txt"]
    assert "oldest copy" in asker.asked[-1][1]
    assert run(ctx, "move_duplicates", folder=str(box)).startswith("No duplicate")        # the _duplicates folder is not scanned again
    assert ctx.journal.undo().startswith("Undid 3 of 3")
    assert (box / "copy of report.txt").exists() and (box / "sub" / "report again.txt").exists() and not (box / "_duplicates").exists()


def test_move_duplicates_denied_and_name_clashes(ctx: Ctx, box: Path, asker: Asker):
    put(box / "a.txt", "dup", age_days=5)
    put(box / "s1" / "b.txt", "dup", age_days=4)
    put(box / "s2" / "b.txt", "dup", age_days=3)
    asker.answer = False
    assert run(ctx, "move_duplicates", folder=str(box)).startswith("DENIED") and (box / "s1" / "b.txt").exists()
    asker.answer = True
    run(ctx, "move_duplicates", folder=str(box))
    assert sorted(p.name for p in (box / "_duplicates").iterdir()) == ["b (2).txt", "b.txt"]
    assert run(ctx, "find_duplicates", folder=str(box), kind="nonsense").startswith("ERROR")


# ---------------------------------------------------------------- tidy

def test_tidy_by_type_moves_files_and_one_undo_restores_everything(ctx: Ctx, box: Path):
    for n in ("a.docx", "b.docx", "photo.jpg", "sheet.xlsx", "readme", ".hidden"):
        put(box / n)
    (box / "keep").mkdir()
    out = run(ctx, "tidy_folder", folder=str(box))
    assert out.startswith("OK: sorted 5 file(s)")
    assert (box / "Word documents" / "a.docx").exists() and (box / "Images" / "photo.jpg").exists() and (box / "Spreadsheets" / "sheet.xlsx").exists()
    assert (box / "Text & data" / "readme").exists() and (box / ".hidden").exists() and (box / "keep").is_dir()
    assert ctx.journal.undo().startswith("Undid")
    assert {p.name for p in box.iterdir()} == {"a.docx", "b.docx", "photo.jpg", "sheet.xlsx", "readme", ".hidden", "keep"}


def test_tidy_by_date_and_extension_and_skips_clashes(ctx: Ctx, box: Path):
    put(box / "old.txt", age_days=400)
    put(box / "new.csv")
    month = time.strftime("%Y-%m", time.localtime(time.time()))
    assert run(ctx, "tidy_folder", folder=str(box), by="date").startswith("OK: sorted 2")
    assert (box / month / "new.csv").exists() and len([p for p in box.iterdir() if p.is_dir()]) == 2
    put(box / "x.txt")
    put(box / "TXT" / "x.txt", "already there")
    out = run(ctx, "tidy_folder", folder=str(box), by="extension")
    assert (box / "x.txt").exists() and "already exist" in out
    assert run(ctx, "tidy_folder", folder=str(box), by="colour").startswith("ERROR")


def test_tidy_nothing_and_denied(ctx: Ctx, box: Path, asker: Asker):
    assert run(ctx, "tidy_folder", folder=str(box)).startswith("Nothing to tidy")
    put(box / "a.txt")
    asker.answer = False
    assert run(ctx, "tidy_folder", folder=str(box)).startswith("DENIED") and (box / "a.txt").exists()


# ---------------------------------------------------------------- bulk rename

def test_rename_with_a_numbered_template_keeps_extensions_and_undoes(ctx: Ctx, box: Path):
    for i, n in enumerate(("IMG_9.jpg", "IMG_2.jpg", "IMG_5.jpg")):
        put(box / n, age_days=10 - i)
    put(box / "notes.txt")
    out = run(ctx, "bulk_rename", folder=str(box), pattern="*.jpg", template="Trip {n:02}", start=1)
    assert out.startswith("OK: renamed 3") and {p.name for p in box.iterdir()} == {"Trip 01.jpg", "Trip 02.jpg", "Trip 03.jpg", "notes.txt"}
    assert (box / "Trip 01.jpg").exists()
    assert ctx.journal.undo().startswith("Undid 3")
    assert {p.name for p in box.iterdir()} == {"IMG_9.jpg", "IMG_2.jpg", "IMG_5.jpg", "notes.txt"}


def test_rename_find_replace_case_prefix_and_date(ctx: Ctx, box: Path):
    put(box / "My Report FINAL.txt")
    assert run(ctx, "bulk_rename", folder=str(box), find=" FINAL", replace_with="", case="lower", prefix="2026 ").startswith("OK")
    assert (box / "2026 my report.txt").exists()
    day = time.strftime("%Y-%m-%d")
    run(ctx, "bulk_rename", folder=str(box), template="{date} {name}")
    assert (box / f"{day} 2026 my report.txt").exists()


def test_rename_refuses_clashes_bad_names_and_empty_instructions(ctx: Ctx, box: Path, asker: Asker):
    put(box / "a.txt")
    put(box / "b.txt")
    assert "same new name" in run(ctx, "bulk_rename", folder=str(box), template="same")
    put(box / "taken.txt")
    assert "already exists" in run(ctx, "bulk_rename", folder=str(box), pattern="a.txt", template="taken")
    assert run(ctx, "bulk_rename", folder=str(box), template="bad/name").startswith("ERROR")
    assert "say how to rename" in run(ctx, "bulk_rename", folder=str(box))
    assert run(ctx, "bulk_rename", folder=str(box), prefix="").startswith("ERROR")
    assert run(ctx, "bulk_rename", folder=str(box), pattern="zzz*", prefix="x").startswith("Nothing to rename")
    asker.answer = False
    assert run(ctx, "bulk_rename", folder=str(box), pattern="a.txt", prefix="x").startswith("DENIED") and (box / "a.txt").exists()


def test_rename_can_swap_names_without_overwriting(ctx: Ctx, box: Path):
    put(box / "a.txt", "A")
    put(box / "b.txt", "B", age_days=1)
    run(ctx, "bulk_rename", folder=str(box), template="{n}", start=1)       # b is older, so it becomes 1 and a becomes 2
    assert (box / "1.txt").read_text() == "B" and (box / "2.txt").read_text() == "A"
    run(ctx, "bulk_rename", folder=str(box), find="1", replace_with="2x")
    assert (box / "2x.txt").read_text() == "B" and (box / "2.txt").read_text() == "A"


# ---------------------------------------------------------------- PDF tools

def test_merge_pdfs_in_order_with_a_chosen_name(ctx: Ctx, box: Path, asker: Asker):
    make_pdf(box / "one.pdf", 2, "alpha")
    make_pdf(box / "two.pdf", 1, "beta")
    out = run(ctx, "pdf_merge", sources="two; one", destination=str(box), name="joined")
    assert out.startswith("OK") and "3 pages" in out
    text = [p.extract_text() for p in PdfReader(str(box / "joined.pdf")).pages]
    assert "beta 1" in text[0] and "alpha 2" in text[2]
    assert "1. two.pdf" in asker.asked[-1][1]
    assert "already exists" in run(ctx, "pdf_merge", sources="one.pdf\ntwo.pdf", destination=str(box), name="joined")
    assert run(ctx, "pdf_merge", sources="one.pdf", destination=str(box)).startswith("ERROR")
    (box / "n.txt").write_text("x")
    assert "not a PDF" in run(ctx, "pdf_merge", sources=f"{box / 'one.pdf'}; {box / 'n.txt'}", name="zz")


def test_extract_pages_names_default_and_errors(ctx: Ctx, box: Path):
    make_pdf(box / "book.pdf", 5)
    out = run(ctx, "pdf_extract", source=str(box / "book"), pages="2-3")
    assert out.startswith("OK") and (box / "book pages 2-3.pdf").exists()
    got = PdfReader(str(box / "book pages 2-3.pdf"))
    assert len(got.pages) == 2 and "page 2" in (got.pages[0].extract_text() or "")
    assert run(ctx, "pdf_extract", source=str(box / "book.pdf"), pages="9").startswith("ERROR")
    assert run(ctx, "pdf_extract", source=str(box / "book.pdf"), pages="x-y").startswith("ERROR")
    assert len(PdfReader(str(box / "book.pdf")).pages) == 5


def test_images_become_a_pdf_one_per_page(ctx: Ctx, box: Path):
    from PIL import Image
    Image.new("RGB", (200, 100), "red").save(box / "a.png")
    Image.new("RGBA", (100, 200), (0, 0, 255, 128)).save(box / "b.png")
    out = run(ctx, "images_to_pdf", sources=f"{box / 'a.png'}; {box / 'b.png'}", name="album")
    assert out.startswith("OK") and len(PdfReader(str(box / "album.pdf")).pages) == 2
    (box / "bad.png").write_text("not an image")
    assert run(ctx, "images_to_pdf", sources=str(box / "bad.png"), name="oops").startswith("ERROR")
    assert not (box / "oops.pdf").exists()


def test_pdf_tools_say_what_to_install_when_a_package_is_missing(ctx: Ctx, box: Path, monkeypatch: pytest.MonkeyPatch):
    real = tools_more.importlib.util.find_spec
    monkeypatch.setattr(tools_more.importlib.util, "find_spec", lambda n, *a: None if n in ("pypdf", "PIL") else real(n, *a))
    assert "pip install pypdf" in run(ctx, "pdf_merge", sources="a; b")
    assert "pip install pillow" in run(ctx, "images_to_pdf", sources="a")


# ---------------------------------------------------------------- Word template

def make_template(path: Path) -> None:
    paras = [docxio.Para([docxio.Run("Dear ", bold=False), docxio.Run("{{name}}", bold=True, size=14, color="1F3A63"), docxio.Run(",")]),
             docxio.Para([docxio.Run("Your session is on {date} at [time] for <<place>>.")])]
    docxio.create_rich(str(path), paras)


def test_fill_template_keeps_formatting_and_leaves_the_original(ctx: Ctx, box: Path):
    make_template(box / "letter.docx")
    original = (box / "letter.docx").read_bytes()
    out = run(ctx, "fill_template", source=str(box / "letter"), values="name=Asha; date=12 Nov 2026; time=5 pm; place=Room 4")
    assert out.startswith("OK") and "4 placeholder(s)" in out
    filled = box / "letter filled.docx"
    text = "\n".join(docxio.paragraph_texts(docxio.load(str(filled))))
    assert "Dear Asha," in text and "12 Nov 2026 at 5 pm for Room 4" in text and "{" not in text
    xml = docxio.load(str(filled))
    assert 'w:val="1F3A63"' in xml and "<w:b/>" in xml and '<w:sz w:val="28"/>' in xml        # the bold navy name kept its look
    assert (box / "letter.docx").read_bytes() == original


def test_fill_template_with_json_missing_values_and_wrong_names(ctx: Ctx, box: Path):
    make_template(box / "letter.docx")
    out = run(ctx, "fill_template", source=str(box / "letter"), values=json.dumps({"name": "Ravi"}), name="partial")
    assert out.startswith("OK") and "Not filled: date, time, place" in out
    assert "Placeholders in this template: name, date, time, place" in run(ctx, "fill_template", source=str(box / "letter"))
    assert "none of those names" in run(ctx, "fill_template", source=str(box / "letter"), values="colour=red")
    assert run(ctx, "fill_template", source=str(box / "partial.docx"), values="x=1", name="partial").startswith("ERROR")
    (box / "t.txt").write_text("{a}")
    assert "Word (.docx)" in run(ctx, "fill_template", source=str(box / "t.txt"), values="a=1")


# ---------------------------------------------------------------- new spreadsheet

def test_spreadsheet_from_typed_rows_has_real_numbers_and_bold_header(ctx: Ctx, box: Path):
    out = run(ctx, "create_spreadsheet", rows="Name,Qty,Phone\nAsha,3,0123\n\"Rao, K\",4.5,99", destination=str(box), name="stock")
    assert out.startswith("OK") and "1 sheet(s) and 9 cell(s)" in out
    import openpyxl
    ws = openpyxl.load_workbook(box / "stock.xlsx").active
    assert [c.value for c in ws[1]] == ["Name", "Qty", "Phone"] and ws["A1"].font.b
    assert ws["B2"].value == 3 and ws["B3"].value == 4.5 and ws["C2"].value == "0123" and ws["A3"].value == "Rao, K"
    assert "already exists" in run(ctx, "create_spreadsheet", rows="a,b", destination=str(box), name="stock")


def test_spreadsheet_from_csv_and_another_workbook(ctx: Ctx, box: Path):
    (box / "data.csv").write_text("City;Pop\nChennai;11\nPune;7\n", encoding="utf-8")
    assert run(ctx, "create_spreadsheet", source=str(box / "data.csv"), name="cities").startswith("OK")
    assert [s.get(3, 1) for s in sheets.load(str(box / "cities.xlsx"))] == ["Pune"]
    sheets.create_xlsx(str(box / "two.xlsx"), [("A", [["x"], ["1"]]), ("B", [["y"], ["2"]])])
    run(ctx, "create_spreadsheet", source=str(box / "two.xlsx"), sheet="B", name="justb")
    assert [s.name for s in sheets.load(str(box / "justb.xlsx"))] == ["B"]


def test_spreadsheet_from_a_word_table_and_choosing_a_table(ctx: Ctx, box: Path):
    d = Document()
    for rows in ([["h1", "h2"], ["a", "1"]], [["k", "v"], ["z", "9"]]):
        t = d.add_table(rows=2, cols=2)
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                t.cell(r, c).text = v
        d.add_paragraph("between")
    d.save(box / "tables.docx")
    run(ctx, "create_spreadsheet", source=str(box / "tables"), name="all")
    assert [s.name for s in sheets.load(str(box / "all.xlsx"))] == ["Table 1", "Table 2"]
    run(ctx, "create_spreadsheet", source=str(box / "tables.docx"), table=2, name="second")
    assert sheets.load(str(box / "second.xlsx"))[0].get(2, 1) == "z"
    assert "table must be 1 to 2" in run(ctx, "create_spreadsheet", source=str(box / "tables.docx"), table=5, name="nope")
    docxio.create(str(box / "plain.docx"), ["no table here"])
    assert "no table was found" in run(ctx, "create_spreadsheet", source=str(box / "plain.docx"), name="none")
    assert run(ctx, "create_spreadsheet").startswith("ERROR")
    (box / "p.png").write_bytes(b"x")
    assert run(ctx, "create_spreadsheet", source=str(box / "p.png"), name="bad").startswith("ERROR")


def test_spreadsheet_from_a_pdf_table(ctx: Ctx, box: Path):
    pytest.importorskip("pdfplumber")
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    t = Table([["Item", "Cost"], ["Pen", "5"], ["Book", "40"]])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    SimpleDocTemplate(str(box / "prices.pdf")).build([t])
    assert run(ctx, "create_spreadsheet", source=str(box / "prices.pdf"), name="prices").startswith("OK")
    sh = sheets.load(str(box / "prices.xlsx"))[0]
    assert sh.get(1, 1) == "Item" and sh.get(3, 2) == "40"


# ---------------------------------------------------------------- zip files

def make_zip(path: Path) -> Path:
    docxio.create(str(path.parent / "inner.docx"), ["Contract renewal on 20 Nov 2026"])
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("readme.txt", "hello from the archive\nsecond line")
        z.writestr("docs/notes.txt", "the budget is 500")
        z.write(path.parent / "inner.docx", "docs/inner.docx")
        z.writestr("img/pic.png", b"\x89PNG\0\0")
    (path.parent / "inner.docx").unlink()
    return path


def test_zip_list_read_and_search(ctx: Ctx, box: Path):
    make_zip(box / "pack.zip")
    out = run(ctx, "zip_contents", path=str(box / "pack"))
    assert "4 file(s)" in out and "docs/notes.txt" in out
    assert "hello from the archive" in run(ctx, "zip_contents", path=str(box / "pack.zip"), member="readme.txt")
    assert "Contract renewal" in run(ctx, "zip_contents", path=str(box / "pack.zip"), member="inner.docx")
    assert "not a text" in run(ctx, "zip_contents", path=str(box / "pack.zip"), member="pic.png")
    assert "not inside" in run(ctx, "zip_contents", path=str(box / "pack.zip"), member="missing.txt")
    found = run(ctx, "zip_contents", path=str(box / "pack.zip"), query="budget")
    assert "1 file(s)" in found and "docs/notes.txt" in found and "was not found" in run(ctx, "zip_contents", path=str(box / "pack.zip"), query="zebra")
    (box / "x.txt").write_text("no")
    assert "not a .zip" in run(ctx, "zip_contents", path=str(box / "x.txt"))
    (box / "bad.zip").write_bytes(b"PK\x03\x04broken")
    assert run(ctx, "zip_contents", path=str(box / "bad.zip")).startswith("ERROR")


def test_extract_zip_into_a_new_folder_and_undo(ctx: Ctx, box: Path, asker: Asker):
    make_zip(box / "pack.zip")
    out = run(ctx, "extract_zip", source=str(box / "pack"))
    assert out.startswith("OK") and (box / "pack" / "docs" / "notes.txt").read_text() == "the budget is 500"
    assert "4 file(s)" in asker.asked[-1][1]
    assert "already exists" in run(ctx, "extract_zip", source=str(box / "pack.zip"))
    run(ctx, "extract_zip", source=str(box / "pack.zip"), members="notes.txt", name="just notes")
    assert [p.name for p in (box / "just notes").rglob("*") if p.is_file()] == ["notes.txt"]
    assert ctx.journal.undo().startswith("Removed") and not (box / "just notes").exists() and (box / "pack").exists()
    assert "no file in the archive matches" in run(ctx, "extract_zip", source=str(box / "pack.zip"), members="nothing", name="zero")


def test_extract_zip_refuses_unsafe_archives(ctx: Ctx, box: Path):
    with zipfile.ZipFile(box / "evil.zip", "w") as z:
        z.writestr("../outside.txt", "gotcha")
        z.writestr("fine.txt", "ok")
    assert "unsafe path" in run(ctx, "extract_zip", source=str(box / "evil.zip")) and not (box.parent / "outside.txt").exists() and not (box / "evil").exists()
    with zipfile.ZipFile(box / "abs.zip", "w") as z:
        z.writestr("/etc/passwd", "x")
    assert "unsafe path" in run(ctx, "extract_zip", source=str(box / "abs.zip"))
    with zipfile.ZipFile(box / "link.zip", "w") as z:
        info = zipfile.ZipInfo("shortcut")
        info.external_attr = 0o120777 << 16
        z.writestr(info, "/etc")
    assert "holds a link" in run(ctx, "extract_zip", source=str(box / "link.zip"))
    (box / "plain.txt").write_text("not an archive")
    assert "is not a .zip" in run(ctx, "extract_zip", source=str(box / "plain.txt"))


def test_extract_zip_refuses_a_bomb_and_cleans_up_on_failure(ctx: Ctx, box: Path, monkeypatch: pytest.MonkeyPatch):
    with zipfile.ZipFile(box / "bomb.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("zeros.bin", b"\0" * 60_000_000)
    assert "too big or too strongly compressed" in run(ctx, "extract_zip", source=str(box / "bomb.zip"))
    make_zip(box / "pack.zip")
    monkeypatch.setattr(tools_more.shutil, "copyfileobj", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
    assert run(ctx, "extract_zip", source=str(box / "pack.zip")).startswith("ERROR") and not (box / "pack").exists()


# ---------------------------------------------------------------- history and undo

def test_history_lists_changes_and_undo_reverses_the_latest(ctx: Ctx, box: Path, asker: Asker):
    assert "has not changed anything" in run(ctx, "show_history")
    assert run(ctx, "undo_change").startswith("There is nothing to undo")
    put(box / "a.docx")
    put(box / "b.docx")
    run(ctx, "tidy_folder", folder=str(box))
    run(ctx, "make_folder", path=str(box / "fresh"))
    out = run(ctx, "show_history", limit=5)
    assert "created" in out and "changed 2 file(s) at once" in out and "<- undo would reverse this" in out
    assert out.index("created") < out.index("changed 2")                                    # newest first
    assert run(ctx, "undo_change").startswith("OK") and not (box / "fresh").exists()
    assert run(ctx, "undo_change").startswith("OK: Undid 3") and (box / "a.docx").exists()
    assert "(undone)" in run(ctx, "show_history", today=True)
    assert "Undo:" in asker.asked[-1][0]
    asker.answer = False
    put(box / "c.txt")
    ctx.journal.record("create", path=str(box / "c.txt"))
    assert run(ctx, "undo_change").startswith("DENIED") and (box / "c.txt").exists()


def test_undo_reports_a_stale_entry_and_the_batch_summary(ctx: Ctx, box: Path):
    put(box / "a.txt")
    ctx.journal.record("batch", items=[{"op": "move", "src": str(box / "gone1"), "dst": str(box / "gone2")},
                                       {"op": "create", "path": str(box / "a.txt")}])
    out = run(ctx, "undo_change")
    assert out.startswith("OK: Undid 1 of 2") and "could not be undone" in out
    ctx.journal.record("batch", items=[{"op": "create", "path": str(box / "nope")}, {"op": "create", "path": str(box / "nope2")}])
    assert run(ctx, "undo_change").startswith("ERROR")


# ---------------------------------------------------------------- dates in documents

def test_deadlines_in_the_coming_days_in_several_formats(ctx: Ctx, box: Path):
    today = dt.date.today()
    d1, d2, d3 = today + dt.timedelta(days=5), today + dt.timedelta(days=12), today + dt.timedelta(days=20)
    put(box / "rent.txt", f"Rent is due on {d1.isoformat()} without fail.")
    put(box / "ancient.txt", "Long ago 2001-01-01 nothing.")
    put(box / "visa.txt", f"Passport renewal deadline: {d2.day:02d}/{d2.month:02d}/{d2.year}")
    put(box / "trip.txt", f"We fly on {d3.day} {d3.strftime('%B')} {d3.year}")
    put(box / "later.txt", f"Back in {(today + dt.timedelta(days=200)).isoformat()}")
    put(box / "us.txt", f"Meeting {d1.strftime('%b')} {d1.day}, {d1.year}")
    out = run(ctx, "find_deadlines", folder=str(box), days=30)
    assert out.index(d1.strftime("%d %b %Y")) < out.index(d2.strftime("%d %b %Y")) < out.index(d3.strftime("%d %b %Y"))
    assert "rent.txt" in out and "visa.txt" in out and "trip.txt" in out and "us.txt" in out
    assert "ancient.txt" not in out and "later.txt" not in out and "[deadline word]" in out
    assert "day/month/year" in out and ctx.found
    assert run(ctx, "find_deadlines", folder=str(box), days=3).startswith("No dates between")


def test_deadlines_can_include_the_past_and_filter_by_kind(ctx: Ctx, box: Path):
    ago = dt.date.today() - dt.timedelta(days=4)
    put(box / "old.txt", f"Invoice was due {ago.isoformat()}")
    docxio.create(str(box / "w.docx"), [f"Exam on {(dt.date.today() + dt.timedelta(days=2)).isoformat()}"])
    assert run(ctx, "find_deadlines", folder=str(box)).count("old.txt") == 0
    both = run(ctx, "find_deadlines", folder=str(box), past=True)
    assert "old.txt" in both and "w.docx" in both
    only = run(ctx, "find_deadlines", folder=str(box), kind="word", past=True)
    assert "w.docx" in only and "old.txt" not in only
    assert run(ctx, "find_deadlines", folder=str(box), kind="what").startswith("ERROR")


def test_deadlines_ask_before_reading_a_folder_that_is_not_open(ctx: Ctx, tmp_path: Path, asker: Asker):
    other = tmp_path / "elsewhere"
    put(other / "a.txt", f"due {(dt.date.today() + dt.timedelta(days=1)).isoformat()}")
    asker.choice = "Don't allow"
    assert run(ctx, "find_deadlines", folder=str(other)).startswith("DENIED")
    asker.choice = "Allow this folder until Hearth closes"
    assert "a.txt" in run(ctx, "find_deadlines", folder=str(other))
    asker.answer = True
    assert "a.txt" in run(ctx, "find_deadlines")                                             # now under the readable roots


def test_date_reader_ignores_impossible_dates_and_reads_day_first():
    today = dt.date(2026, 11, 10)
    got = [d for d, _ in tools_more._dates("31/02/2026 and 05/11/2026 and 2026-13-01 and 3rd Dec and Nov 20th", today)]
    assert dt.date(2026, 11, 5) in got and dt.date(2026, 12, 3) in got and dt.date(2026, 11, 20) in got and len(got) == 3


# ---------------------------------------------------------------- the model's side of it

def test_the_new_tools_are_in_the_prompt_and_their_requests_count_as_changes(ctx: Ctx):
    prompt = Agent(ScriptedModel([]), ctx).system_prompt()
    for name in ("compare_files", "find_duplicates", "move_duplicates", "tidy_folder", "bulk_rename", "pdf_merge", "pdf_extract",
                 "images_to_pdf", "fill_template", "create_spreadsheet", "zip_contents", "extract_zip", "show_history", "undo_change",
                 "find_deadlines"):
        assert name in prompt and name in TOOLS
    assert "{{placeholders}}" not in prompt and "{placeholders}" in prompt
    for text in ("tidy up my downloads", "undo that", "unzip pack.zip", "extract pages 2-3 of it", "join these pdfs", "dedupe this folder"):
        assert wants_change(text), text
    for text in ("which files are duplicates?", "compare a.txt and b.txt", "what is due this week"):
        assert not wants_change(text), text


def test_a_question_cannot_tidy_or_rename(ctx: Ctx, box: Path):
    put(box / "a.docx")
    m = ScriptedModel([json.dumps({"tool": "tidy_folder", "args": {"folder": str(box)}}), json.dumps({"answer": "It has one Word file."})])
    res = Agent(m, ctx).run("what is in the box folder?")
    assert (box / "a.docx").exists() and not (box / "Word documents").exists()
    assert res.answer.startswith("It has one Word file.") and res.answer.endswith("Nothing was changed.")      # the refusal is stated


def test_tidy_then_undo_through_the_agent(ctx: Ctx, box: Path):
    put(box / "a.docx")
    put(box / "p.pdf")
    m = ScriptedModel([json.dumps({"tool": "tidy_folder", "args": {"folder": str(box)}}), json.dumps({"answer": "Sorted 2 files."}),
                       json.dumps({"tool": "undo_change", "args": {}}), json.dumps({"answer": "Undone."})])
    agent = Agent(m, ctx)
    assert agent.run("tidy up the box folder").answer == "Sorted 2 files." and (box / "PDFs" / "p.pdf").exists()
    assert agent.run("undo that").answer == "Undone." and (box / "p.pdf").exists() and not (box / "PDFs").exists()
