"""PDF -> Word keeping the look: fonts, sizes, bold/italic, colours, alignment, bullets, right-hand dates, hyperlinks, pages."""
import os
import re
import shutil
import zipfile
from pathlib import Path

import pytest
from conftest import Asker
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas

from hearth import docxio, pdfrich
from hearth.tools import Ctx, run_tool

pytestmark = pytest.mark.skipif(not pdfrich.available(), reason="needs pdfminer.six")


def make_pdf(path: Path, pages: int = 2) -> None:
    """A little resume-like page: centred bold title, a link, a bullet dot, a date on the right, then a second page."""
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFillColor(Color(0.12, 0.23, 0.39))
    c.setFont("Helvetica-Bold", 22)
    c.drawCentredString(297.5, 780, "JANE DOE")
    c.setFillColor(Color(0, 0, 0))
    c.setFont("Times-Roman", 11)
    c.drawString(60, 740, "Visit my site")
    c.linkURL("https://example.com/jane", (60, 736, 130, 752), relative=0)
    c.setFont("Times-Italic", 11)
    c.drawString(60, 710, "Italic remark about the work")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(60, 680, "Engineer at Acme")
    c.setFont("Helvetica", 11)
    c.drawRightString(535, 680, "June 2025")
    c.setFillColor(Color(0, 0, 0))
    c.circle(66, 654, 2, fill=1, stroke=0)                       # a drawn bullet dot, like Chrome and Word make
    c.setFont("Helvetica", 11)
    c.drawString(80, 650, "Built the thing and shipped it")
    c.showPage()
    if pages > 1:
        c.setFont("Helvetica", 11)
        c.drawString(60, 780, "Second page text")
        c.showPage()
    c.save()


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    p = tmp_path / "resume.pdf"
    make_pdf(p)
    return p


def test_formatting_survives_the_extraction(pdf: Path):
    r = pdfrich.extract(str(pdf), [1, 2])
    by = {p.text(): p for p in r.paras}
    title = by["JANE DOE"]
    assert title.align == "center" and title.runs[0].bold and title.runs[0].size == 22.0
    assert title.runs[0].font == "Arial" and title.runs[0].color == "1F3B63"
    site = by["Visit my site"]
    assert site.runs[0].font == "Times New Roman" and site.runs[0].url == "https://example.com/jane"
    assert by["Italic remark about the work"].runs[0].italic
    job = next(p for t, p in by.items() if t.startswith("Engineer at Acme"))
    assert job.right_tab and job.text() == "Engineer at Acme\tJune 2025"
    bullet = next(p for t, p in by.items() if "Built the thing" in t)
    assert bullet.text().startswith("•\t") and bullet.hanging >= 6 and bullet.indent > 10
    assert r.paras[-1].text() == "Second page text" and r.paras[-1].page_break and not r.paras[0].page_break
    assert r.links == 1 and r.pages == 2 and r.page == (595.0, 842.0)


def test_the_word_file_really_has_the_formatting(pdf: Path, tmp_path: Path):
    r = pdfrich.extract(str(pdf), [1, 2])
    out = tmp_path / "out.docx"
    assert docxio.create_rich(str(out), r.paras, r.page, r.margins) == 1
    doc = Document(str(out))                                      # python-docx opens it: the package is valid
    title = doc.paragraphs[0]
    assert title.alignment == WD_ALIGN_PARAGRAPH.CENTER
    run = title.runs[0]
    assert run.bold and run.font.size.pt == 22 and run.font.name == "Arial" and str(run.font.color.rgb) == "1F3B63"
    link = doc.paragraphs[1].hyperlinks[0]
    assert link.url == "https://example.com/jane" and link.text == "Visit my site"
    assert any(r_.italic for p in doc.paragraphs for r_ in p.runs)
    assert doc.sections[0].page_width.pt == 595 and doc.sections[0].page_height.pt == 842
    xml = zipfile.ZipFile(out).read("word/document.xml").decode()
    assert "pageBreakBefore" in xml and 'w:val="right"' in xml and "w:hanging" in xml and "<w:tab/>" in xml


def test_repeated_links_share_one_relationship_and_odd_text_is_escaped(tmp_path: Path):
    paras = [docxio.Para([docxio.Run("a & <b>", url="https://x.test/?a=1&b=2"), docxio.Run(" tail")]),
             docxio.Para([docxio.Run("again", url="https://x.test/?a=1&b=2")])]
    out = tmp_path / "l.docx"
    assert docxio.create_rich(str(out), paras) == 1
    d = Document(str(out))
    assert d.paragraphs[0].hyperlinks[0].url == "https://x.test/?a=1&b=2" and d.paragraphs[0].text == "a & <b> tail"
    assert d.paragraphs[1].hyperlinks[0].url == "https://x.test/?a=1&b=2"


@pytest.mark.parametrize("name, family, bold, italic", [
    ("ABCDEF+Calibri-Bold", "Calibri", True, False), ("Helvetica-BoldOblique", "Arial", True, True),
    ("Times-Roman", "Times New Roman", False, False), ("TimesNewRomanPS-BoldItalicMT", "Times New Roman", True, True),
    ("Carlito", "Calibri", False, False), ("Liberation Sans", "Arial", False, False), ("Fira Sans", "Fira Sans", False, False), ("Arial,Bold", "Arial", True, False), ("", None, False, False),
])
def test_font_names(name: str, family: str | None, bold: bool, italic: bool):
    assert pdfrich.font_style(name) == (family, bold, italic)


@pytest.mark.parametrize("colour, expected", [
    ((0.12, 0.23, 0.39), "1F3B63"), (0.4, "666666"), (0.0, None), (1.0, None), ((1.0, 1.0, 1.0), None),
    ((1, 0, 0, 0), "00FFFF"), (None, None), ("x", None), ((1, 2), None),
])
def test_colours(colour, expected):
    assert pdfrich.to_hex(colour) == expected


# ---- through the copy_to_word tool

def run(ctx: Ctx, **a):
    return run_tool(ctx, "copy_to_word", a)


def test_copy_to_word_keeps_formatting_and_says_so(ctx: Ctx, root: Path, pdf: Path, asker: Asker):
    shutil.copy(pdf, root / "resume.pdf")
    out = run(ctx, source=str(root / "resume.pdf"), destination=str(root), name="second run")
    assert out.startswith("OK") and "second run.docx" in out and "Formatting kept" in out and "1 link(s)" in out
    assert "Not copied: images (0)" in out
    assert "Formatting kept" in asker.asked[-1][1]                     # the approval card says what will be kept
    d = Document(str(root / "second run.docx"))
    assert d.paragraphs[0].runs[0].bold and d.paragraphs[1].hyperlinks[0].url == "https://example.com/jane"


def test_plain_text_on_request_and_when_the_package_is_missing(ctx: Ctx, root: Path, pdf: Path, monkeypatch: pytest.MonkeyPatch):
    out = run(ctx, source=str(pdf), destination=str(root / "a.docx"), plain=True)
    assert out.startswith("OK") and "Plain text only (plain text was asked for)" in out
    assert not Document(str(root / "a.docx")).paragraphs[0].runs[0].bold
    monkeypatch.setattr(pdfrich, "available", lambda: False)
    out = run(ctx, source=str(pdf), destination=str(root / "b.docx"))
    assert out.startswith("OK") and "pdfminer.six" in out and "Plain text only" in out
    assert "JANE DOE" in Document(str(root / "b.docx")).paragraphs[0].text


def test_adding_to_an_existing_document_stays_plain_and_says_why(ctx: Ctx, root: Path, pdf: Path):
    assert run(ctx, text="first", destination=str(root / "e.docx")).startswith("OK")
    out = run(ctx, source=str(pdf), destination=str(root / "e.docx"))
    assert out.startswith("OK") and "only kept when the document is new" in out
    assert "JANE DOE" in [p.text for p in Document(str(root / "e.docx")).paragraphs]


def test_a_word_source_is_copied_exactly(ctx: Ctx, root: Path, tmp_path: Path):
    src = root / "styled.docx"
    d = Document()
    d.add_heading("Title", 1).runs[0].font.size = None
    para = d.add_paragraph()
    para.add_run("bold").bold = True
    d.save(src)
    out = run(ctx, source=str(src), destination=str(root), name="copy of it.doc")
    assert out.startswith("OK") and "Copied exactly" in out and (root / "copy of it.docx").read_bytes() == src.read_bytes()
    again = run(ctx, source=str(src), destination=str(root), name="copy of it")    # exists now: added, never overwritten
    assert again.startswith("OK") and "added" in again
    assert run(ctx, source=str(src), destination=str(root), name="bad/name").startswith("ERROR")
    assert os.path.exists(root / "styled.docx")


def test_unreadable_layout_falls_back_to_plain_text(ctx: Ctx, root: Path, monkeypatch: pytest.MonkeyPatch, pdf: Path):
    def boom(path, pages):
        raise pdfrich.RichError("no text with a layout was found")
    monkeypatch.setattr(pdfrich, "extract", boom)
    out = run(ctx, source=str(pdf), destination=str(root / "f.docx"))
    assert out.startswith("OK") and "Plain text only (no text with a layout was found)" in out


def test_page_ranges_and_limits(ctx: Ctx, root: Path, pdf: Path):
    out = run(ctx, source=str(pdf), pages="2", destination=str(root / "p2.docx"))
    d = Document(str(root / "p2.docx"))
    assert out.startswith("OK") and [p.text for p in d.paragraphs] == ["Second page text"]
    with pytest.raises(pdfrich.RichError):
        pdfrich.extract(str(pdf), list(range(1, pdfrich.MAX_PAGES + 2)))


def test_a_pdf_that_is_not_a_pdf_is_a_clean_error(ctx: Ctx, root: Path):
    bad = root / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 nonsense")
    with pytest.raises(pdfrich.RichError):
        pdfrich.extract(str(bad), [1])
    assert run(ctx, source=str(bad), destination=str(root / "g.docx")).startswith("ERROR")


# ---- the look beyond fonts: web addresses that are only underlined text, rules, line spacing, page count

def make_resume_like(path: Path) -> None:
    """Like a Chrome-printed resume: underlined web addresses with no link inside, rules under headings, tight lines."""
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    navy = Color(0.12, 0.23, 0.39)
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(297.5, 790, "R. ADVAITH")
    c.setFont("Helvetica", 9)
    c.drawString(172, 770, "linkedin.com/in/advaith")
    c.setStrokeColor(navy)
    c.setLineWidth(0.8)
    c.line(172, 768, 172 + c.stringWidth("linkedin.com/in/advaith", "Helvetica", 9), 768)                                    # an underline, not a link annotation
    c.setFillColor(Color(0.4, 0.4, 0.4))
    c.drawString(280, 770, "  |  ")
    c.setFillColor(Color(0, 0, 0))
    c.drawString(300, 770, "mail me: jane@example.com")
    c.setFillColor(navy)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(30, 740, "SUMMARY")
    c.setFillColor(Color(0.8, 0.8, 0.8))
    c.rect(30, 735, 535, 0.8, fill=1, stroke=0)                  # the grey rule under a heading
    c.setFillColor(Color(0, 0, 0))
    c.setFont("Helvetica", 9)
    for i in range(3):
        c.drawString(30, 720 - 11.5 * i, f"Body line number {i + 1} that is long enough to look like running text here.")
    c.showPage()
    c.save()


def test_underlined_addresses_become_links_and_emails_become_mailto(tmp_path: Path):
    pdf = tmp_path / "r.pdf"
    make_resume_like(pdf)
    rich = pdfrich.extract(str(pdf), [1])
    links = {r.text: r.url for p in rich.paras for r in p.runs if r.url}
    assert links == {"linkedin.com/in/advaith": "https://linkedin.com/in/advaith", "jane@example.com": "mailto:jane@example.com"}
    under = {r.text: r.underline for p in rich.paras for r in p.runs if r.url}
    assert under["linkedin.com/in/advaith"] is True and under["jane@example.com"] is False     # only what the page underlined
    assert rich.links == 2


def test_rules_become_borders_and_lines_keep_their_distance(tmp_path: Path):
    pdf = tmp_path / "r.pdf"
    make_resume_like(pdf)
    rich = pdfrich.extract(str(pdf), [1])
    heading = next(p for p in rich.paras if p.text() == "SUMMARY")
    assert heading.border is not None and heading.border[0] == "CCCCCC" and heading.border[1] >= 0.5
    body = [p for p in rich.paras if p.text().startswith("Body line")]
    assert len(body) == 3 and all(abs(p.line + p.before - 11.5) < 0.2 for p in body[1:])     # the page's own distance between lines, not Word's
    assert [p.border for p in body] == [None, None, None]
    out = tmp_path / "r.docx"
    docxio.create_rich(str(out), rich.paras, rich.page, rich.margins)
    xml = zipfile.ZipFile(out).read("word/document.xml").decode()
    assert 'w:lineRule="exact"' in xml and "<w:pBdr><w:bottom" in xml and 'w:color="CCCCCC"' in xml
    assert re.search(r"<w:pPr><w:pBdr>.*?</w:pBdr><w:spacing", xml)                    # Word wants this order
    assert "<w:u " in xml and xml.count("<w:hyperlink") == 2


def test_the_copy_does_not_run_to_a_second_page(tmp_path: Path):
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        pytest.skip("needs LibreOffice")
    import subprocess

    from pypdf import PdfReader
    pdf = tmp_path / "r.pdf"
    make_resume_like(pdf)
    rich = pdfrich.extract(str(pdf), [1])
    docxio.create_rich(str(tmp_path / "c.docx"), rich.paras, rich.page, rich.margins)
    subprocess.run([soffice, "--headless", "-env:UserInstallation=file://" + str(tmp_path / "prof"), "--convert-to", "pdf", "--outdir",
                    str(tmp_path), str(tmp_path / "c.docx")], capture_output=True, timeout=120, check=False)
    assert len(PdfReader(str(tmp_path / "c.pdf")).pages) == 1


@pytest.mark.parametrize("text,url", [("see https://a.org/x, ok", "https://a.org/x"), ("www.site.com now", "https://www.site.com"),
                                      ("write to me@mail.com.", "mailto:me@mail.com")])
def test_linkify_finds_addresses_in_running_text(text: str, url: str):
    out = pdfrich._linkify([docxio.Run(text)])
    assert [r.url for r in out if r.url] == [url] and "".join(r.text for r in out) == text.replace(".com.", ".com.")


def test_a_plain_word_with_a_dot_is_not_a_link():
    out = pdfrich._linkify([docxio.Run("version 2.5 is fine. Done.")])
    assert [r.url for r in out] == [None]
