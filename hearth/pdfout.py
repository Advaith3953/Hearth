"""Word and text files to PDF. Three ways, best first, and the answer always says which one was used:
1. Microsoft Word itself (Windows, if Word is installed): the same layout Word shows.
2. LibreOffice, if installed: also a faithful layout.
3. Built in (needs reportlab): rebuilt from the file's paragraphs, close but not identical. Standard PDF fonts only, so
   non-Latin text becomes '?', tables become one line per row, pictures are skipped."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

from . import docxio

CONVERTIBLE = (".docx", ".txt", ".md", ".log", ".csv", ".tsv", ".json", ".xml", ".ini", ".cfg", ".yml", ".yaml")
TEXTY = CONVERTIBLE[1:]

_WORD_SCRIPT = (
    "$ErrorActionPreference='Stop'; $w=New-Object -ComObject Word.Application; $w.Visible=$false; $w.DisplayAlerts=0; "
    "try { $d=$w.Documents.Open($env:HEARTH_SRC,$false,$true); $d.ExportAsFixedFormat($env:HEARTH_DST,17); $d.Close($false) } "
    "finally { $w.Quit() }")


class PdfError(Exception):
    pass


def _run(cmd: list[str], env: dict[str, str] | None = None, timeout: int = 120) -> bool:
    try:
        done = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout, check=False)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _word(src: str, dst: str) -> bool:
    if sys.platform != "win32":
        return False
    env = {**os.environ, "HEARTH_SRC": src, "HEARTH_DST": dst}
    ok = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WORD_SCRIPT], env)
    return ok and os.path.exists(dst) and os.path.getsize(dst) > 0


def _soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", "")):
        cand = os.path.join(base, "LibreOffice", "program", "soffice.exe") if base else ""
        if cand and os.path.exists(cand):
            return cand
    return None


def _libreoffice(src: str, dst: str) -> bool:
    exe = _soffice()
    if not exe:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        profile = "file:///" + os.path.join(tmp, "profile").replace("\\", "/").lstrip("/")
        if not _run([exe, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "pdf", "--outdir", tmp, src]):
            return False
        made = os.path.join(tmp, os.path.splitext(os.path.basename(src))[0] + ".pdf")
        if not (os.path.exists(made) and os.path.getsize(made) > 0):
            return False
        shutil.copyfile(made, dst)
    return True


# ---- the built-in writer

def _face(family: str | None, bold: bool, italic: bool) -> str:
    f = (family or "").lower()
    if any(k in f for k in ("times", "georgia", "garamond", "cambria", "palatino", "serif", "book")):
        return {(0, 0): "Times-Roman", (1, 0): "Times-Bold", (0, 1): "Times-Italic", (1, 1): "Times-BoldItalic"}[(bold, italic)]
    if any(k in f for k in ("courier", "consolas", "mono", "menlo")):
        return {(0, 0): "Courier", (1, 0): "Courier-Bold", (0, 1): "Courier-Oblique", (1, 1): "Courier-BoldOblique"}[(bold, italic)]
    return {(0, 0): "Helvetica", (1, 0): "Helvetica-Bold", (0, 1): "Helvetica-Oblique", (1, 1): "Helvetica-BoldOblique"}[(bold, italic)]


_LOOKALIKES = {"\u2192": "->", "\u2190": "<-", "\u21d2": "=>", "\u2265": ">=", "\u2264": "<=", "\u2248": "~", "\u2212": "-",
               "\u00a0": " ", "\u2009": " ", "\u202f": " ", "\u2713": "v", "\u2714": "v"}


def _safe(text: str) -> str:
    """Standard PDF fonts only cover Western European letters; anything else would print as a black box."""
    for ch, plain in _LOOKALIKES.items():
        text = text.replace(ch, plain)
    return escape(text.encode("cp1252", "replace").decode("cp1252"))


def _markup(runs: list[docxio.Run]) -> str:
    out = []
    for r in runs:
        size = r.size or 11.0
        color = r.color or ("0563C1" if r.url else "000000")
        inner = f'<font name="{_face(r.font, r.bold, r.italic)}" size="{size}" color="#{color}">{_safe(r.text)}</font>'
        if r.url:
            inner = f'<a href="{escape(r.url, {chr(34): "&quot;"})}"><u>{inner}</u></a>'
        out.append(inner)
    return "".join(out)


def _reportlab(paras: list[docxio.Para], page: tuple[float, float], margins: tuple[float, float, float, float], dst: str) -> None:
    try:
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import BaseDocTemplate, Frame, PageBreak, PageTemplate, Paragraph, Spacer, Table
    except ImportError as e:
        raise PdfError("this needs Microsoft Word or LibreOffice, or the reportlab package: python -m pip install reportlab") from e
    top, right, bottom, left = margins
    width = page[0] - left - right
    story: list[object] = []
    align = {"left": TA_LEFT, "center": TA_CENTER, "right": TA_RIGHT}
    for p in paras:
        if p.page_break and story:
            story.append(PageBreak())
        runs = [r for r in p.runs if r.text]
        bullet = None
        if runs and runs[0].text == "•\t":
            bullet, runs = "•", runs[1:]
        size = max((r.size or 11.0 for r in runs), default=11.0)
        style = ParagraphStyle("p", fontName="Helvetica", fontSize=size, leading=size * 1.2, alignment=align.get(p.align, TA_LEFT),
                               leftIndent=p.indent, firstLineIndent=0 if bullet else -p.hanging if p.hanging else 0,
                               bulletIndent=max(0.0, p.indent - p.hanging), spaceBefore=min(p.before, 60))
        if not runs:
            story.append(Spacer(1, size * 1.2))
            continue
        left_runs, right_runs = runs, []
        if p.right_tab:                      # text, a tab, then something flush right (a date)
            for i, r in enumerate(runs):
                if "\t" in r.text:
                    before, _, after = r.text.partition("\t")
                    left_runs = [*runs[:i], docxio.Run(before, r.bold, r.italic, r.size, r.font, r.color, r.url)]
                    right_runs = [docxio.Run(after, r.bold, r.italic, r.size, r.font, r.color, r.url), *runs[i + 1:]]
                    break
        for r in [*left_runs, *right_runs]:
            r.text = r.text.replace("\t", "    ")
        if right_runs:
            rstyle = ParagraphStyle("r", parent=style, alignment=TA_RIGHT, leftIndent=0, firstLineIndent=0)
            story.append(Table([[Paragraph(_markup(left_runs), style, bulletText=bullet), Paragraph(_markup(right_runs), rstyle)]],
                               colWidths=[width * 0.78, width * 0.22], style=[("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        else:
            story.append(Paragraph(_markup(left_runs), style, bulletText=bullet))
    if not story:
        story.append(Spacer(1, 12))
    doc = BaseDocTemplate(dst, pagesize=page)
    frame = Frame(left, bottom, width, page[1] - top - bottom, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame])])
    doc.build(story)


def _text_paras(path: str) -> list[docxio.Para]:
    from . import readers
    try:
        text = readers.read_text(path)
    except readers.UnreadableError as e:
        raise PdfError(str(e)) from e
    return [docxio.Para([docxio.Run(line.expandtabs(4), size=10.0, font="Courier")]) for line in text.split("\n")]


def convert(src: str, dst: str) -> str:
    """Write dst (a .pdf that does not exist yet) from src. Returns the name of the engine that did it."""
    ext = os.path.splitext(src)[1].lower()
    if ext not in CONVERTIBLE:
        raise PdfError(f"cannot make a PDF from a {ext or 'extensionless'} file; Word (.docx) and text files work")
    tmp = dst + ".hearth-tmp"
    try:
        if ext == ".docx":
            if _word(src, tmp):
                engine = "Microsoft Word"
            elif _libreoffice(src, tmp):
                engine = "LibreOffice"
            else:
                try:
                    paras, page, margins = docxio.read_rich(src)
                except docxio.DocxError as e:
                    raise PdfError(str(e)) from e
                _reportlab(paras, page, margins, tmp)
                engine = "Hearth's built-in writer (close to the original, not identical)"
        else:
            _reportlab(_text_paras(src), (595.0, 842.0), (54.0, 54.0, 54.0, 54.0), tmp)
            engine = "Hearth's built-in text writer"
        os.replace(tmp, dst)
        return engine
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
