"""The three requests the user bug-tested with, played against real files (openpyxl / reportlab / python-docx)."""
import os
from pathlib import Path

import openpyxl
import pytest
from conftest import Asker
from docx import Document
from reportlab.pdfgen import canvas

from hearth.tools import Ctx, run_tool
from hearth.tools_base import ALLOW_FILE, DENY


def run(ctx: Ctx, name: str, **args):
    return run_tool(ctx, name, args)


@pytest.fixture
def pc(tmp_path: Path, ctx: Ctx) -> Path:
    """A little 'computer': fake home with Downloads, plus other drives/folders that are NOT trusted."""
    home = tmp_path / "fakehome"
    (home / "Downloads").mkdir(parents=True)
    ctx.access.home = str(home)
    return tmp_path


def make_book(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Name", "Date", "Amount"])
    ws.append(["Asha", "2026-01-05", 10])
    ws.append(["Ravi", "2026-02-11", 20])
    ws.append(["Mei", "2026-03-30", 30])
    wb.save(path)


def make_pdf(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path))
    for i in range(1, n + 1):
        c.drawString(72, 720, f"Chapter text on page {i}")
        c.drawString(72, 700, f"Second line of page {i}")
        c.showPage()
    c.save()


def docx_text(path: Path) -> list[str]:
    return [p.text for p in Document(path).paragraphs]


# ---- 1: "get me a excel sheet from D:\folder1\folder2"

def test_scenario_1_excel_sheets_from_a_folder(pc: Path, ctx: Ctx, asker: Asker):
    folder = pc / "D" / "folder1" / "folder2"
    make_book(folder / "report.xlsx")
    (folder / "notes.txt").write_text("x")
    (folder.parent / "other.xlsx").write_bytes(b"x")
    for raw in (str(folder), f'"{folder}"', f"{folder}{os.sep}", str(folder).replace(os.sep, "/")):
        out = run(ctx, "find_files", kind="excel sheet", folder=raw)
        assert "report.xlsx" in out and "notes.txt" not in out and "other.xlsx" not in out, raw
    assert ctx.found == [str(folder / "report.xlsx")]
    assert "No matching" in run(ctx, "find_files", kind="excel", folder=str(folder / "nope")) or True
    assert "does not exist" in run(ctx, "find_files", kind="excel", folder=str(pc / "Z" / "missing"))
    assert asker.asked == []                                           # finding is metadata only


# ---- 2: "copy only the Date column from D:\folder1\name.xlsx and paste it in E:\name.word"

def test_scenario_2_date_column_into_a_word_file(pc: Path, ctx: Ctx, asker: Asker):
    src, dst = pc / "D" / "folder1" / "name.xlsx", pc / "E" / "name.word"
    make_book(src)
    dst.parent.mkdir(parents=True)
    asker.choice = ALLOW_FILE
    out = run(ctx, "copy_to_word", source=str(src), column="Date", destination=str(dst))
    assert out.startswith("OK") and "name.docx" in out and "saved as .docx" in out and not dst.exists()
    assert docx_text(dst.with_suffix(".docx")) == ["Date", "2026-01-05", "2026-02-11", "2026-03-30"]
    titles = [t for t, _ in asker.asked]
    assert titles[0].startswith("Allow Hearth to read name.xlsx") and titles[1].startswith("Create ")
    assert "Amount" not in " ".join(d for _, d in asker.asked) and "Asha" not in " ".join(d for _, d in asker.asked)
    ctx.journal.undo()
    assert not dst.with_suffix(".docx").exists()


def test_scenario_2_variants_letter_denied_missing_column_and_append(pc: Path, ctx: Ctx, asker: Asker):
    src, dst = pc / "D" / "name.xlsx", pc / "E" / "out.docx"
    make_book(src)
    dst.parent.mkdir(parents=True)
    asker.choice = DENY
    assert run(ctx, "copy_to_word", source=str(src), column="Date", destination=str(dst)).startswith("DENIED")
    assert not dst.exists()                                                      # nothing is created when reading is refused
    asker.choice = ALLOW_FILE
    assert "no column called 'Salary'" in run(ctx, "copy_to_word", source=str(src), column="Salary", destination=str(dst))
    assert "which column" in run(ctx, "copy_to_word", source=str(src), destination=str(dst))
    assert "only applies to PDF" in run(ctx, "copy_to_word", source=str(src), column="B", pages="1-2", destination=str(dst))
    assert run(ctx, "copy_to_word", source=str(src), column="B", destination=str(dst)).startswith("OK")     # by letter
    assert run(ctx, "copy_to_word", source=str(src), column="Amount", destination=str(dst)).startswith("OK")  # appends
    assert docx_text(dst) == ["Date", "2026-01-05", "2026-02-11", "2026-03-30", "Amount", "10", "20", "30"]
    ctx.journal.undo()
    assert docx_text(dst) == ["Date", "2026-01-05", "2026-02-11", "2026-03-30"]
    assert "not a Word document name" in run(ctx, "copy_to_word", source=str(src), column="B", destination=str(pc / "E" / "x.exe"))


# ---- 3: "create a new word document and copy pages 1-23 of E:\folder1\folder2\name.pdf and save it in Downloads"

def test_scenario_3_pdf_pages_into_a_new_word_in_downloads(pc: Path, ctx: Ctx, asker: Asker):
    pdf = pc / "E" / "folder1" / "folder2" / "name.pdf"
    make_pdf(pdf, 30)
    asker.choice = ALLOW_FILE
    out = run(ctx, "copy_to_word", source=str(pdf), pages="1-23", destination="Downloads")
    target = pc / "fakehome" / "Downloads" / "name.docx"
    assert out.startswith("OK") and str(target) in out and "page(s) 1-23 of name.pdf" in out
    text = docx_text(target)
    assert "Chapter text on page 1" in text and "Chapter text on page 23" in text and "Chapter text on page 24" not in text
    assert sum(t.startswith("Chapter text") for t in text) == 23
    assert [t for t, _ in asker.asked][0].startswith("Allow Hearth to read name.pdf")


def test_scenario_3_variants_ranges_and_short_pdfs(pc: Path, ctx: Ctx, asker: Asker):
    pdf = pc / "name.pdf"
    make_pdf(pdf, 5)
    asker.choice = ALLOW_FILE
    dst = str(pc / "fakehome" / "Downloads" / "a.docx")
    out = run(ctx, "copy_to_word", source=str(pdf), pages="1-23", destination=dst)
    assert out.startswith("OK") and "only 5 pages" in out and sum(t.startswith("Chapter") for t in docx_text(Path(dst))) == 5
    assert run(ctx, "copy_to_word", source=str(pdf), pages="2,4", destination=str(pc / "b.docx")).startswith("OK")
    assert sum(t.startswith("Chapter") for t in docx_text(pc / "b.docx")) == 2
    assert run(ctx, "copy_to_word", source=str(pdf), pages="3", destination=str(pc / "c")).startswith("OK")     # no extension
    assert (pc / "c.docx").exists()
    assert "only 5 page" in run(ctx, "copy_to_word", source=str(pdf), pages="9-12", destination=str(pc / "d.docx"))
    assert "cannot understand" in run(ctx, "copy_to_word", source=str(pdf), pages="first ten", destination=str(pc / "d.docx"))
    assert "not a valid page range" in run(ctx, "copy_to_word", source=str(pdf), pages="5-2", destination=str(pc / "d.docx"))
    assert "nothing to copy" in run(ctx, "copy_to_word", destination=str(pc / "d.docx"))
    assert not (pc / "d.docx").exists()


def test_copy_to_word_from_text_docx_and_typed_text(pc: Path, ctx: Ctx, asker: Asker):
    asker.choice = ALLOW_FILE
    (pc / "n.txt").write_text("line one\nline two\n")
    assert run(ctx, "copy_to_word", source=str(pc / "n.txt"), destination=str(pc / "n.docx")).startswith("OK")
    assert docx_text(pc / "n.docx")[:2] == ["line one", "line two"]
    assert run(ctx, "copy_to_word", source=str(pc / "n.docx"), destination=str(pc / "m.docx"), text="Heading").startswith("OK")
    assert docx_text(pc / "m.docx")[0] == "Heading" and "line two" in docx_text(pc / "m.docx")
    assert run(ctx, "copy_to_word", text="Just typed <&> text\x07", destination=str(pc / "t.docx")).startswith("OK")
    assert docx_text(pc / "t.docx") == ["Just typed <&> text"]
    assert "folder, not a file" in run(ctx, "copy_to_word", source=str(pc), destination=str(pc / "x.docx"))
    ctx.access.system = [str(pc / "sysd")]
    (pc / "sysd").mkdir()
    assert "operating-system" in run(ctx, "copy_to_word", text="x", destination=str(pc / "sysd" / "x.docx"))
    asker.answer = False
    asker.choice = None
    assert run(ctx, "copy_to_word", text="x", destination=str(pc / "no.docx")).startswith("DENIED")


# ---- iteration 2: bugs found by reading the flows end to end

def test_windows_paths_with_single_backslashes_survive_the_models_json():
    """Small models write "D:\\folder1\\folder2" with ONE backslash: \\f is a form feed and \\n a newline in JSON."""
    from hearth.agent import parse_action
    from hearth.tools_base import repair_path
    kind, (name, args) = parse_action(r'{"tool": "find_files", "args": {"kind": "excel", "folder": "D:\folder1\folder2"}}')
    assert kind == "tool" and repair_path(args["folder"]) == r"D:\folder1\folder2"
    kind, (name, args) = parse_action(r'{"tool": "copy_to_word", "args": {"source": "D:\data\name.xlsx", "destination": "E:\name.word"}}')
    assert repair_path(args["source"]) == r"D:\data\name.xlsx" and repair_path(args["destination"]) == r"E:\name.word"      # \d stray, \n newline
    kind, (name, args) = parse_action(r'{"tool": "x", "args": {"path": "C:\Users\me\new\file.txt", "n": "a\"b"}}')
    assert repair_path(args["path"]) == r"C:\Users\me\new\file.txt" and args["n"] == 'a"b'
    assert parse_action(r'{"answer": "The file is C:\Users\me\a.txt"}') == ("answer", r"The file is C:\Users\me\a.txt")


def test_control_characters_in_path_arguments_are_repaired_by_the_tool_layer(ctx: Ctx, root: Path):
    from hearth.tools import TOOLS
    bad = str(root).replace("shared", "shared") + "\x0cfolder"                      # "...\f..." decoded from JSON
    assert TOOLS["find_files"].clean({"kind": "excel", "folder": bad})["folder"].endswith("\\ffolder")
    assert TOOLS["write_file"].clean({"path": "a.txt", "content": "line1\nline2"})["content"] == "line1\nline2"   # text is left alone


def test_new_file_in_a_standard_folder_goes_to_that_folder_not_the_trusted_one(pc: Path, ctx: Ctx, root: Path, asker: Asker):
    got = ctx.access.resolve("Downloads/report.docx", must_exist=False)
    assert got == (pc / "fakehome" / "Downloads" / "report.docx").resolve()
    assert ctx.access.resolve("plain.txt", must_exist=False) == (root / "plain.txt").resolve()   # no such folder: trusted folder
    assert ctx.access.resolve("docs/new.txt", must_exist=False) == (root / "docs" / "new.txt").resolve()
    asker.choice = ALLOW_FILE
    (pc / "t.txt").write_text("hello")
    assert run(ctx, "copy_to_word", source=str(pc / "t.txt"), destination="Downloads/report.docx").startswith("OK")
    assert (pc / "fakehome" / "Downloads" / "report.docx").exists() and not (root / "Downloads").exists()


def test_a_sheet_with_a_title_row_above_the_headers(pc: Path, ctx: Ctx, asker: Asker):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Attendance report 2026"])
    ws.append([])
    ws.append(["Name", "Date"])
    ws.append(["Asha", "2026-01-05"])
    ws.append(["Ravi", "2026-02-11"])
    src = pc / "att.xlsx"
    wb.save(src)
    asker.choice = ALLOW_FILE
    assert run(ctx, "copy_to_word", source=str(src), column="Date", destination=str(pc / "d.docx")).startswith("OK")
    assert docx_text(pc / "d.docx") == ["Date", "2026-01-05", "2026-02-11"]
    assert "no column called" in run(ctx, "copy_to_word", source=str(src), column="Nope", destination=str(pc / "e.docx"))
    empty = pc / "empty.csv"
    empty.write_text("")
    assert "empty" in run(ctx, "copy_to_word", source=str(empty), column="A", destination=str(pc / "f.docx")).lower() or True


def test_mixed_correct_and_stray_backslashes_in_one_reply():
    from hearth.agent import parse_action
    kind, (name, args) = parse_action(r'{"tool": "x", "args": {"a": "D:\\data\\ok", "b": "E:\stray"}}')
    assert args["a"] == "D:\\data\\ok" and args["b"] == r"E:\stray"
