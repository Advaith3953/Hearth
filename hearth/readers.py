"""Reading file contents as text. Plain text files and .docx are supported; anything binary is refused."""
from __future__ import annotations

import html
import os
import re
import zipfile

MAX_READ = 2_000_000
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log",
    ".xml", ".html", ".htm", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".sh",
    ".bat", ".sql", ".tex", ".org", ".css",
}
DOC_EXTS = {".docx"}
SHEET_BOOK_EXTS = {".xlsx", ".xlsm"}
PDF_EXTS = {".pdf"}


class UnreadableError(Exception):
    pass


def ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def indexable(path: str) -> bool:
    e = ext(path)
    return e in TEXT_EXTS or e in DOC_EXTS or e in SHEET_BOOK_EXTS or e in PDF_EXTS or e == ""


def editable(path: str) -> bool:
    """Files Hearth may create or modify: plain text only (rewriting a .docx or .pdf as text would corrupt it)."""
    e = ext(path)
    return e in TEXT_EXTS or e == ""


def _docx(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > 30_000_000:
                raise UnreadableError("document is too large")
            xml = z.read(info).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise UnreadableError("not a readable .docx file") from e
    xml = xml.replace("</w:p>", "\n").replace("<w:tab/>", "\t").replace("<w:br/>", "\n")
    return html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()


def _pdf(path: str) -> str:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found,unused-ignore]
    except ImportError as e:
        raise UnreadableError("reading PDFs needs one extra package. Install it with:  pip install pypdf") from e
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise UnreadableError("this PDF is password-protected")
        parts = []
        for n, page in enumerate(reader.pages[:2000], start=1):
            parts.append(f"[page {n}]\n{(page.extract_text() or '').strip()}")
    except UnreadableError:
        raise
    except Exception as e:  # pypdf raises many kinds of errors on damaged files
        raise UnreadableError(f"could not read this PDF ({type(e).__name__})") from e
    text = "\n\n".join(parts)
    if not re.sub(r"\[page \d+\]|\s", "", text):
        raise UnreadableError("this PDF has no selectable text (it may be a scan of paper)")
    return text


def pdf_pages(path: str) -> list[str]:
    """The text of every page of a PDF (index 0 = page 1)."""
    text = _pdf(path)
    pages = re.split(r"(?m)^\[page (\d+)\]\n", text)
    # re.split gives ['', '1', text1, '2', text2, ...]
    return [pages[i + 1].strip() for i in range(1, len(pages) - 1, 2)]


def parse_pages(spec: str, total: int) -> list[int]:
    """'1-23', '5', '1,3,5-7' or 'all' -> sorted page numbers within 1..total. Raises UnreadableError when impossible."""
    spec = spec.strip().lower().replace("pages", "").replace("page", "").replace("to", "-").replace("–", "-").strip()
    if spec in ("", "all"):
        return list(range(1, total + 1))
    out: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise UnreadableError(f"cannot understand the page range '{spec}'; write it like 1-23 or 2,5,7-9")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if a < 1 or b < a:
            raise UnreadableError(f"'{part}' is not a valid page range")
        out.update(range(a, b + 1))
    if min(out) > total:
        raise UnreadableError(f"the document has only {total} page(s)")
    return sorted(p for p in out if p <= total)


def read_text(path: str, limit: int = MAX_READ) -> str:
    e = ext(path)
    if e in DOC_EXTS:
        return _docx(path)
    if e in PDF_EXTS:
        return _pdf(path)
    if e in SHEET_BOOK_EXTS:
        from . import sheets
        try:
            return sheets.to_text(path)
        except sheets.SheetError as err:
            raise UnreadableError(str(err)) from err
    try:
        size = os.path.getsize(path)
        if size > limit:
            raise UnreadableError(f"file is too large to read ({size:,} bytes)")
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise UnreadableError(str(e)) from e
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\0" in data[:4096]:
        raise UnreadableError("this looks like a binary file (image, program, archive...), not text")
    return data.decode("utf-8", errors="replace")


def read_text_strict(path: str) -> str:
    """For files that will be modified: refuse anything that would not round-trip byte for byte."""
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_READ + 1)
    except OSError as e:
        raise UnreadableError(str(e)) from e
    if len(data) > MAX_READ:
        raise UnreadableError("file is too large to edit")
    if b"\0" in data[:4096] or data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise UnreadableError("not a UTF-8 text file; editing it could damage it")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise UnreadableError("not a UTF-8 text file; editing it could damage it") from e
