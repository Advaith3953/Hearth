"""'second run' -> 'third check' as a PDF: names without an extension, and Word/text files turned into PDFs."""
import json
import os
import zipfile
from pathlib import Path

import pytest
from conftest import Asker
from docx import Document
from pypdf import PdfReader

from hearth import docxio, pdfout
from hearth.access import AccessError
from hearth.agent import Agent
from hearth.model import ScriptedModel
from hearth.tools import Ctx, run_tool
from hearth.tools_base import ALLOW_FILE

HAVE_SOFFICE = pdfout._soffice() is not None


def tool(tool_name: str, /, **args) -> str:
    return json.dumps({"tool": tool_name, "args": args})


def answer(text: str) -> str:
    return json.dumps({"answer": text})


def run(ctx: Ctx, **a):
    return run_tool(ctx, "save_as_pdf", a)


@pytest.fixture
def pc(tmp_path: Path, ctx: Ctx) -> Path:
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    ctx.access.home = str(home)
    (tmp_path / "E").mkdir()
    return tmp_path


def make_docx(path: Path) -> None:
    paras = [docxio.Para([docxio.Run("R. ADVAITH", bold=True, size=20, font="Arial", color="1F3A63")], align="center"),
             docxio.Para([docxio.Run("my site", url="https://example.com/me"), docxio.Run(" | Chennai")], align="center", before=4),
             docxio.Para([docxio.Run("\u2022\t"), docxio.Run("Built a thing")], indent=18, hanging=12),
             docxio.Para([docxio.Run("Cyber Analyst", bold=True), docxio.Run("\tJune 2025")], right_tab=True),
             docxio.Para([docxio.Run("arrow \u2192 and \u0b95 and \u00e9")], page_break=True)]
    docxio.create_rich(str(path), paras)


@pytest.fixture
def second_run(pc: Path) -> Path:
    p = pc / "E" / "second run.docx"
    make_docx(p)
    return p


# ---- names without an extension

def test_a_name_without_extension_finds_the_file(pc: Path, ctx: Ctx, second_run: Path):
    assert ctx.access.resolve(str(pc / "E" / "second run")) == second_run.resolve()
    assert ctx.access.resolve(str(pc / "E" / "SECOND RUN")) == second_run.resolve()          # case does not matter


def test_a_bare_name_is_found_in_the_usual_places(pc: Path, ctx: Ctx):
    (pc / "home" / "Downloads" / "memo.txt").write_text("x")
    assert ctx.access.resolve("memo") == (pc / "home" / "Downloads" / "memo.txt").resolve()


def test_several_matches_are_narrowed_by_kind_or_listed(pc: Path, ctx: Ctx, second_run: Path):
    (pc / "E" / "second run.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(AccessError, match="several files match"):
        ctx.access.resolve(str(pc / "E" / "second run"))
    assert ctx.access.resolve(str(pc / "E" / "second run"), prefer=(".docx", ".txt")) == second_run.resolve()
    with pytest.raises(AccessError, match="several files match"):
        ctx.access.resolve(str(pc / "E" / "second run"), prefer=(".docx", ".pdf"))


def test_a_folder_is_not_matched_by_stem(pc: Path, ctx: Ctx):
    (pc / "E" / "thing").mkdir()
    assert ctx.access.resolve(str(pc / "E" / "thing")).name == "thing"
    with pytest.raises(AccessError):
        ctx.access.resolve(str(pc / "E" / "nothing-here"))


# ---- save_as_pdf

def pdf_text(path: Path) -> str:
    return " ".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)


def test_your_conversation_ends_with_third_check_pdf(pc: Path, ctx: Ctx, asker: Asker, second_run: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pdfout, "_libreoffice", lambda s, d: False)
    asker.choice = ALLOW_FILE
    m = ScriptedModel([tool("save_as_pdf", source=str(pc / "E" / "second run"), destination=str(pc / "E"), name="third check"),
                       answer("Created third check.pdf in E:.")])
    res = Agent(m, ctx).run('now from "second run" convert it back into a pdf in the same drive under the name "third check"')
    out = pc / "E" / "third check.pdf"
    assert out.exists() and not (pc / "E" / "third check.docx").exists() and "Nothing was changed" not in res.answer
    assert "R. ADVAITH" in pdf_text(out)


@pytest.mark.skipif(not HAVE_SOFFICE, reason="needs LibreOffice")
def test_libreoffice_engine(pc: Path, ctx: Ctx, second_run: Path):
    out = run(ctx, source=str(second_run), name="lo")
    assert out.startswith("OK") and "LibreOffice" in out
    assert "R. ADVAITH" in pdf_text(pc / "E" / "lo.pdf") and len(PdfReader(str(pc / "E" / "lo.pdf")).pages) == 2


def test_builtin_engine_keeps_look_links_and_pages(pc: Path, ctx: Ctx, second_run: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pdfout, "_libreoffice", lambda s, d: False)
    out = run(ctx, source=str(second_run), destination=str(pc / "E" / "built in"))
    assert out.startswith("OK") and "built-in writer" in out and (pc / "E" / "built in.pdf").exists()
    reader = PdfReader(str(pc / "E" / "built in.pdf"))
    assert len(reader.pages) == 2                                           # the page break survived
    text = pdf_text(pc / "E" / "built in.pdf")
    assert "R. ADVAITH" in text and "June 2025" in text and "arrow -> and ? and" in text and "Built a thing" in text
    uris = [a.get_object()["/A"]["/URI"] for pg in reader.pages for a in (pg.get("/Annots") or [])]
    assert uris == ["https://example.com/me"]
    fonts = {f for pg in reader.pages for f in pg["/Resources"]["/Font"].values() for f in [f.get_object()["/BaseFont"]]}
    assert "/Helvetica-Bold" in fonts


def test_word_is_tried_first_on_windows(pc: Path, ctx: Ctx, second_run: Path, monkeypatch: pytest.MonkeyPatch):
    seen = {}

    def fake_run(cmd, env=None, timeout=120):
        seen.update(cmd=cmd, src=env["HEARTH_SRC"], dst=env["HEARTH_DST"])
        Path(env["HEARTH_DST"]).write_bytes(b"%PDF-1.4 from word")
        return True
    monkeypatch.setattr(pdfout.sys, "platform", "win32")
    monkeypatch.setattr(pdfout, "_run", fake_run)
    out = run(ctx, source=str(second_run), name="viaword")
    assert out.startswith("OK") and "Microsoft Word" in out and seen["cmd"][0] == "powershell"
    assert seen["src"] == str(second_run.resolve()) and (pc / "E" / "viaword.pdf").read_bytes() == b"%PDF-1.4 from word"
    assert "ExportAsFixedFormat" in pdfout._WORD_SCRIPT and not (pc / "E" / "viaword.pdf.hearth-tmp").exists()
    monkeypatch.setattr(pdfout, "_run", lambda *a, **k: False)               # Word fails -> the next engine takes over
    monkeypatch.setattr(pdfout, "_libreoffice", lambda s, d: False)
    assert "built-in writer" in run(ctx, source=str(second_run), name="fallback")


def test_text_files_and_unsupported_files(pc: Path, ctx: Ctx):
    (pc / "home" / "Downloads" / "memo.txt").write_text("line one\n\tindented line\nthird é", encoding="utf-8")
    assert run(ctx, source="memo", destination=str(pc / "E")).startswith("OK")
    assert "line one" in pdf_text(pc / "E" / "memo.pdf")
    (pc / "E" / "sheet.xlsx").write_bytes(b"PK")
    assert run(ctx, source=str(pc / "E" / "sheet.xlsx")).startswith("ERROR")
    assert run(ctx, source=str(pc / "E")).startswith("ERROR")                  # a folder


def test_targets_never_overwrite_and_names_are_checked(pc: Path, ctx: Ctx, second_run: Path, asker: Asker, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pdfout, "_libreoffice", lambda s, d: False)
    assert run(ctx, source=str(second_run)).startswith("OK") and (pc / "E" / "second run.pdf").exists()    # next to the source
    assert "already exists" in run(ctx, source=str(second_run))
    assert run(ctx, source=str(second_run), name="bad/name").startswith("ERROR")
    assert run(ctx, source=str(second_run), destination=str(pc / "E" / "x.docx")).startswith("OK")          # extension replaced
    assert (pc / "E" / "x.pdf").exists() and not (pc / "E" / "x.docx.pdf").exists()
    asker.answer = False
    assert run(ctx, source=str(second_run), name="denied").startswith("DENIED") and not (pc / "E" / "denied.pdf").exists()


def test_when_no_engine_exists_the_error_says_what_to_install(pc: Path, ctx: Ctx, second_run: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pdfout, "_libreoffice", lambda s, d: False)
    import builtins
    real = builtins.__import__

    def no_reportlab(name, *a, **k):
        if name.startswith("reportlab"):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_reportlab)
    out = run(ctx, source=str(second_run), name="none")
    assert out.startswith("ERROR") and "pip install reportlab" in out and not (pc / "E" / "none.pdf").exists()


# ---- reading Word files

def test_read_rich_handles_styles_lists_tables_and_links(tmp_path: Path):
    d = Document()
    d.add_heading("Title here", 1)
    d.add_paragraph("plain body")
    d.add_paragraph("item one", style="List Bullet")
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "A", "B"
    p = tmp_path / "w.docx"
    d.save(p)
    paras, page, margins = docxio.read_rich(str(p))
    texts = [x.text() for x in paras]
    assert texts[0] == "Title here" and paras[0].runs[0].size and paras[0].runs[0].size > 12
    assert any(t.startswith("\u2022\titem one") for t in texts) and "A  |  B" in texts
    assert page[0] > 500 and margins[0] > 0
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"nope")
    with pytest.raises(docxio.DocxError):
        docxio.read_rich(str(bad))
    with zipfile.ZipFile(tmp_path / "nobody.docx", "w") as z:
        z.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'/>")
    with pytest.raises(docxio.DocxError, match="no body"):
        docxio.read_rich(str(tmp_path / "nobody.docx"))


# ---- a made-up 'done' cannot stand

def test_a_false_success_claim_gets_a_correction(ctx: Ctx):
    res = Agent(ScriptedModel([answer("The file 'third check.pdf' has been created in E:\\ with the exact formatting.")]), ctx) \
        .run("convert second run into a pdf named third check")
    assert res.answer.endswith("Nothing was changed.")
    ok = Agent(ScriptedModel([answer("It is a Word document with 3 paragraphs.")]), ctx).run("convert second run into a pdf")
    assert "Nothing was changed" not in ok.answer
    q = Agent(ScriptedModel([answer("The file was created yesterday.")]), ctx).run("when was it made?")
    assert "Nothing was changed" not in q.answer                                # a question is left alone
    assert os.path.exists(ctx.access.home)


def test_allow_all_asks_once_for_eight_pdfs(pc: Path, ctx: Ctx, asker: Asker, second_run: Path, monkeypatch: pytest.MonkeyPatch):
    from hearth.tools_base import ALLOW_BATCH
    monkeypatch.setattr(pdfout, "_libreoffice", lambda s, d: False)
    asker.choice = ALLOW_BATCH
    calls = [tool("save_as_pdf", source=str(second_run), destination=str(pc / "E"), name=f"copy {i}") for i in range(1, 9)]
    res = Agent(ScriptedModel(calls + [answer("Made eight PDFs.")]), ctx).run("make pdf copies of second run named copy 1 to copy 8")
    assert all((pc / "E" / f"copy {i}.pdf").exists() for i in range(1, 9)) and "Nothing was changed" not in res.answer
    assert len([q for q in asker.asked if q[0].startswith('Create')]) == 1  # one question, not eight
    assert ctx.batch_ok
    Agent(ScriptedModel([answer("ok")]), ctx).run("what is in E?")
    assert not ctx.batch_ok                                                # the permission ends with the request


def test_plain_allow_still_asks_each_time_and_risky_changes_always_ask(pc: Path, ctx: Ctx, asker: Asker, second_run: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pdfout, "_libreoffice", lambda s, d: False)
    for i in (1, 2):
        assert run(ctx, source=str(second_run), name=f"one {i}").startswith("OK")
    assert len([q for q in asker.asked if q[0].startswith('Create')]) == 2
    ctx.batch_ok = True                                                    # even after "allow all", a delete still asks
    (pc / "E" / "x.txt").write_text("x")
    asker.answer = False
    out = run_tool(ctx, "delete_path", {"path": str(pc / "E" / "x.txt")})
    assert "DENIED" in out and (pc / "E" / "x.txt").exists()


# ---- the real failure: "create 8 pdf copies of second run in a new folder Checking" -------------------------------------

def test_count_is_read_from_the_request():
    from hearth.agent import pdf_copies_asked, wants_change, wants_pdf_out
    assert pdf_copies_asked("create 8 pdf copies of second run") == 8
    assert pdf_copies_asked("I asked for 8 copies of the word as Pdfs in the Checking folder") == 8
    assert pdf_copies_asked("make eight pdfs of it") == 8
    assert pdf_copies_asked("make a pdf of it") == 0 and pdf_copies_asked("copy 8 rows into word") == 0
    assert wants_pdf_out("convert it to a pdf") and not wants_pdf_out("copy this pdf into a word file")
    for retry in ("no I see no copies", "I asked for 8 copies of the word as Pdfs"):
        assert wants_change(retry)


def test_several_pdfs_in_one_call_one_question_one_undo(pc: Path, ctx: Ctx, asker: Asker, second_run: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pdfout, "_libreoffice", lambda s, d: False)
    out = run(ctx, source=str(second_run), destination=str(pc / "E" / "Checking"), copies=8)
    assert out.startswith("OK") and "8 PDFs" in out
    folder = pc / "E" / "Checking"
    assert sorted(p.name for p in folder.iterdir()) == [f"second run {i}.pdf" for i in range(1, 9)]
    assert all("R. ADVAITH" in pdf_text(p) for p in folder.iterdir())
    assert len([q for q in asker.asked if q[0].startswith("Create 8 PDF")]) == 1
    assert "A new folder will be created" in [q for q in asker.asked if q[0].startswith("Create 8 PDF")][0][1]
    ctx.journal.undo()                                                     # ONE undo takes back the folder and all eight files
    assert not folder.exists() or not list(folder.glob("*.pdf"))
    assert second_run.exists()


def test_copies_never_overwrite_and_skip_taken_numbers(pc: Path, ctx: Ctx, second_run: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pdfout, "_libreoffice", lambda s, d: False)
    folder = pc / "E" / "Many"
    folder.mkdir()
    (folder / "pc 2.pdf").write_bytes(b"keep me")
    out = run(ctx, source=str(second_run), destination=str(folder), name="pc", copies=3)
    assert out.startswith("OK") and (folder / "pc 2.pdf").read_bytes() == b"keep me"
    assert sorted(p.name for p in folder.iterdir()) == ["pc 1.pdf", "pc 2.pdf", "pc 3.pdf", "pc 4.pdf"]
    assert run(ctx, source=str(second_run), destination=str(folder), copies=99).startswith("ERROR")


def test_small_model_that_picks_the_wrong_tool_and_forgets_the_count(pc: Path, ctx: Ctx, asker: Asker, second_run: Path, monkeypatch: pytest.MonkeyPatch):
    """The 8B model used copy_to_word for a PDF request, then called save_as_pdf once and wandered off. Hearth fills in the count
    and the right tool itself, and the model's extra calls change nothing."""
    monkeypatch.setattr(pdfout, "_libreoffice", lambda s, d: False)
    asker.choice = ALLOW_FILE
    dest = str(pc / "E" / "Checking")
    m = ScriptedModel([tool("copy_to_word", source=str(second_run), destination=dest, plain=False),
                       tool("save_as_pdf", source=str(pc / "E" / "second run"), destination=dest),
                       tool("save_as_pdf", source=str(pc / "E" / "second run"), destination=dest, name="again"),
                       answer("Created 8 PDFs in E:\\Checking.")])
    res = Agent(m, ctx).run("search for second run file. then copy its contents and formatting and create 8 pdfs in a new folder Checking on E")
    folder = pc / "E" / "Checking"
    assert len(list(folder.glob("*.pdf"))) == 8 and not list(folder.glob("*.docx"))
    assert "Nothing was changed" not in res.answer
    assert len([q for q in asker.asked if q[0].startswith("Create 8 PDF")]) == 1
