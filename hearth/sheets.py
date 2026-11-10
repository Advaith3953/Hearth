"""Spreadsheets without any extra packages: read .xlsx/.xlsm/.csv/.tsv, and change ONE cell in place.

An .xlsx edit patches only that cell's XML inside the original file, so styles, other sheets, charts, images and
formulas are left exactly as they were. Formula cells are never overwritten.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

SHEET_EXTS = {".xlsx", ".xlsm", ".csv", ".tsv"}
MAX_PART = 60_000_000
MAX_ROWS = 200_000
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
BUILTIN_DATE_IDS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59))


class SheetError(Exception):
    pass


@dataclass
class Sheet:
    name: str
    cells: dict[int, dict[int, str]] = field(default_factory=dict)   # row -> column (1-based) -> text
    formulas: set[tuple[int, int]] = field(default_factory=set)
    date_cells: set[tuple[int, int]] = field(default_factory=set)
    part: str = ""                                                   # xlsx: path of the sheet's XML inside the zip

    @property
    def rows(self) -> list[int]:
        return sorted(self.cells)

    @property
    def header_row(self) -> int:
        return self.rows[0] if self.cells else 1

    @property
    def max_col(self) -> int:
        return max((max(r) for r in self.cells.values() if r), default=0)

    def get(self, row: int, col: int) -> str:
        return self.cells.get(row, {}).get(col, "")

    def headers(self, header_row: int | None = None) -> dict[int, str]:
        return dict(sorted(self.cells.get(header_row or self.header_row, {}).items()))


def col_letters(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _ref(ref: str) -> tuple[int, int]:
    m = re.fullmatch(r"([A-Za-z]{1,3})(\d+)", ref)
    if not m:
        raise SheetError(f"unsupported cell reference '{ref}'")
    return int(m.group(2)), col_index(m.group(1))


def _number(text: str) -> str:
    try:
        f = float(text)
    except ValueError:
        return text
    if f != f or f in (float("inf"), float("-inf")):
        return text
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return format(f, ".15g")


def _serial_to_text(text: str, date1904: bool) -> str:
    try:
        f = float(text)
        base = dt.datetime(1904, 1, 1) if date1904 else dt.datetime(1899, 12, 30)
        d = base + dt.timedelta(days=f)
    except (ValueError, OverflowError):
        return text
    return d.strftime("%Y-%m-%d") if f == int(f) else d.strftime("%Y-%m-%d %H:%M")


def _text_of(el: ET.Element) -> str:
    """Text of an <si>/<is>: all <t> except phonetic hints."""
    out = []
    for child in el:
        if child.tag == NS + "t":
            out.append(child.text or "")
        elif child.tag == NS + "r":
            out.extend((t.text or "") for t in child.findall(NS + "t"))
    return "".join(out)


def _read_part(z: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = z.getinfo(name)
    except KeyError as e:
        raise SheetError(f"the workbook has no '{name}'") from e
    if info.file_size > MAX_PART:
        raise SheetError("the workbook is too large")
    return z.read(info)


def _is_date_format(code: str) -> bool:
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code)
    return bool(re.search(r"[dmyhs]", code, re.I))


def _workbook_layout(z: zipfile.ZipFile) -> tuple[list[tuple[str, str]], bool]:
    wb = ET.fromstring(_read_part(z, "xl/workbook.xml"))
    rels = ET.fromstring(_read_part(z, "xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id", ""): r.get("Target", "") for r in rels}
    out: list[tuple[str, str]] = []
    for s in wb.iter(NS + "sheet"):
        tgt = targets.get(s.get(RNS + "id", ""), "")
        if not tgt:
            continue
        part = tgt.lstrip("/") if tgt.startswith("/") else "xl/" + tgt
        out.append((s.get("name", ""), part))
    pr = wb.find(NS + "workbookPr")
    return out, bool(pr is not None and pr.get("date1904") in ("1", "true"))


def _styles(z: zipfile.ZipFile) -> set[int]:
    """Indexes of cell formats that display dates."""
    try:
        root = ET.fromstring(_read_part(z, "xl/styles.xml"))
    except SheetError:
        return set()
    custom = {int(n.get("numFmtId", "-1")): n.get("formatCode", "") for n in root.iter(NS + "numFmt")}
    xfs = root.find(NS + "cellXfs")
    dates: set[int] = set()
    if xfs is not None:
        for i, xf in enumerate(xfs):
            fid = int(xf.get("numFmtId", "0"))
            if fid in BUILTIN_DATE_IDS or (fid in custom and _is_date_format(custom[fid])):
                dates.add(i)
    return dates


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(_read_part(z, "xl/sharedStrings.xml"))
    except SheetError:
        return []
    return [_text_of(si) for si in root.iter(NS + "si")]


def load_xlsx(path: str, headers_only: bool = False) -> list[Sheet]:
    try:
        with zipfile.ZipFile(path) as z:
            layout, date1904 = _workbook_layout(z)
            shared = _shared_strings(z)
            date_xfs = _styles(z)
            sheets = []
            for name, part in layout:
                sheets.append(_load_sheet(z, name, part, shared, date_xfs, date1904, headers_only))
            return sheets
    except (zipfile.BadZipFile, ET.ParseError, OSError, KeyError, ValueError) as e:
        raise SheetError(f"this does not look like a valid Excel file ({type(e).__name__})") from e


def _load_sheet(z: zipfile.ZipFile, name: str, part: str, shared: list[str], date_xfs: set[int], date1904: bool,
                headers_only: bool) -> Sheet:
    sh = Sheet(name, part=part)
    info = z.getinfo(part)
    if info.file_size > MAX_PART:
        raise SheetError("a sheet is too large")
    row_no = 0
    with z.open(info) as f:
        for _, el in ET.iterparse(f, events=("end",)):
            if el.tag != NS + "row":
                continue
            row_no = int(el.get("r") or row_no + 1)
            col_no = 0
            row: dict[int, str] = {}
            for c in el.findall(NS + "c"):
                r = c.get("r")
                if r:
                    _, col_no = _ref(r)
                else:
                    col_no += 1
                t = c.get("t")
                v = c.find(NS + "v")
                raw = (v.text or "") if v is not None else ""
                is_date = int(c.get("s", "0") or 0) in date_xfs
                if c.find(NS + "f") is not None:
                    sh.formulas.add((row_no, col_no))
                if t == "s":
                    text = shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else ""
                elif t == "inlineStr":
                    isel = c.find(NS + "is")
                    text = _text_of(isel) if isel is not None else ""
                elif t == "b":
                    text = "TRUE" if raw == "1" else "FALSE"
                elif t in ("str", "e", "d"):
                    text = raw
                else:
                    text = _serial_to_text(raw, date1904) if (is_date and raw) else _number(raw)
                    if is_date and raw:
                        sh.date_cells.add((row_no, col_no))
                if text != "":
                    row[col_no] = text
            el.clear()
            if row:
                sh.cells[row_no] = row
                if headers_only:
                    break
            if len(sh.cells) > MAX_ROWS:
                raise SheetError("the sheet has too many rows")
    return sh


# ------------------------------------------------------------------ csv

def _csv_text(path: str) -> tuple[str, str]:
    if os.path.getsize(path) > MAX_PART:
        raise SheetError("the file is too large")
    with open(path, "rb") as f:
        data = f.read()
    bom = "﻿" if data.startswith(b"\xef\xbb\xbf") else ""
    try:
        return data.decode("utf-8-sig"), bom
    except UnicodeDecodeError as e:
        raise SheetError("not a UTF-8 text file; it could be damaged by editing") from e


def _dialect(text: str, path: str) -> str:
    if path.lower().endswith(".tsv"):
        return "\t"
    first = text.splitlines()[0] if text.strip() else ""
    counts = {d: first.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def load_csv(path: str, headers_only: bool = False) -> list[Sheet]:
    text, _ = _csv_text(path)
    delim = _dialect(text, path)
    sh = Sheet(os.path.basename(path))
    for i, rec in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delim), start=1):
        row = {c: v for c, v in enumerate(rec, start=1) if v != ""}
        if row:
            sh.cells[i] = row
            if headers_only:
                break
        if i > MAX_ROWS:
            raise SheetError("the file has too many rows")
    return [sh]


def load(path: str, headers_only: bool = False) -> list[Sheet]:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return load_xlsx(path, headers_only)
    if ext in (".csv", ".tsv"):
        return load_csv(path, headers_only)
    raise SheetError("only .xlsx, .xlsm, .csv and .tsv are spreadsheets")


def to_text(path: str, max_chars: int = 2_000_000) -> str:
    out: list[str] = []
    size = 0
    for sh in load(path):
        out.append(f"[Sheet: {sh.name}]")
        for r in sh.rows:
            line = "\t".join(sh.get(r, c) for c in range(1, sh.max_col + 1)).rstrip("\t")
            out.append(line)
            size += len(line) + 1
            if size > max_chars:
                return "\n".join(out)
    return "\n".join(out)


def resolve_column(sh: Sheet, ref: str, header_row: int | None = None) -> int:
    """A column named by its header text ("Age") or by its letter ("C")."""
    heads = sh.headers(header_row)
    want = ref.strip().lower()
    if not want:
        raise SheetError("which column?")
    exact = [c for c, h in heads.items() if h.strip().lower() == want]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise SheetError(f"several columns are called '{ref}'; use the column letter ({', '.join(col_letters(c) for c in exact)})")
    if re.fullmatch(r"[A-Za-z]{1,3}", ref.strip()):
        return col_index(ref)
    part = [c for c, h in heads.items() if want in h.lower()]
    if len(part) == 1:
        return part[0]
    names = ", ".join(f"{h} ({col_letters(c)})" for c, h in heads.items())
    if part:
        raise SheetError(f"'{ref}' matches several columns: " + ", ".join(heads[c] for c in part))
    raise SheetError(f"no column called '{ref}'. Columns: {names}")


# ------------------------------------------------------------------ changing one cell

def _looks_numeric(v: str) -> bool:
    if not re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", v) or len(v) > 15:
        return False
    return not (len(v.lstrip("-")) > 1 and v.lstrip("-").startswith("0") and not v.lstrip("-").startswith("0."))


def _cell_xml(ref: str, style: str, value: str, is_date: bool, as_text: bool = False) -> str:
    s = f' s="{style}"' if style else ""
    if value == "":
        return f'<c r="{ref}"{s}/>'
    if is_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            serial = (dt.datetime.strptime(value, "%Y-%m-%d") - dt.datetime(1899, 12, 30)).days
        except ValueError as e:
            raise SheetError(f"'{value}' is not a valid date") from e
        return f'<c r="{ref}"{s}><v>{serial}</v></c>'
    if _looks_numeric(value) and not as_text:
        return f'<c r="{ref}"{s}><v>{value}</v></c>'
    return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'


def _patch_sheet_xml(xml: str, row: int, col: int, value: str, is_date: bool) -> str:
    ref = f"{col_letters(col)}{row}"
    row_re = re.compile(rf'<row\b(?=[^>]*\br="{row}")[^>]*?(?:/>|>.*?</row>)', re.S)
    m = row_re.search(xml)
    if m is None:
        cell = _cell_xml(ref, "", value, is_date)
        new_row = f'<row r="{row}">{cell}</row>'
        for rm in re.finditer(r'<row\b[^>]*\br="(\d+)"', xml):
            if int(rm.group(1)) > row:
                return xml[:rm.start()] + new_row + xml[rm.start():]
        if "<sheetData/>" in xml:
            return xml.replace("<sheetData/>", f"<sheetData>{new_row}</sheetData>", 1)
        end = xml.rfind("</sheetData>")
        if end < 0:
            raise SheetError("unsupported sheet layout")
        return xml[:end] + new_row + xml[end:]
    row_xml = m.group(0)
    cell_re = re.compile(rf'<c\b(?=[^>]*\br="{ref}")[^>]*?(?:/>|>.*?</c>)', re.S)
    cm = cell_re.search(row_xml)
    if cm is not None:
        old = cm.group(0)
        if "<f" in old:
            raise SheetError(f"{ref} contains a formula; I will not overwrite it")
        head = old[: old.index(">") + 1]
        sm = re.search(r'\bs="(\d+)"', head)
        was_text = re.search(r'\bt="(s|inlineStr|str)"', head) is not None   # IDs/ZIP codes stored as text stay text
        new_row_xml = row_xml.replace(old, _cell_xml(ref, sm.group(1) if sm else "", value, is_date, was_text), 1)
    else:
        cell = _cell_xml(ref, "", value, is_date)
        opening_end = row_xml.index(">")
        opening = re.sub(r'\s+spans="[^"]*"', "", row_xml[: opening_end + 1])
        if opening.endswith("/>"):
            new_row_xml = opening[:-2] + ">" + cell + "</row>"
        else:
            body = row_xml[opening_end + 1: -len("</row>")]
            pos = len(body)
            for cm2 in re.finditer(r'<c\b[^>]*\br="([A-Za-z]+)\d+"', body):
                if col_index(cm2.group(1)) > col:
                    pos = cm2.start()
                    break
            new_row_xml = opening + body[:pos] + cell + body[pos:] + "</row>"
    return xml[: m.start()] + new_row_xml + xml[m.end():]


def _force_recalc(wb_xml: str) -> str:
    if "<calcPr" in wb_xml:
        m = re.search(r"<calcPr\b[^>]*?/?>", wb_xml)
        assert m is not None
        tag = m.group(0)
        if "fullCalcOnLoad" in tag:
            return wb_xml
        return wb_xml.replace(tag, tag.replace("<calcPr", '<calcPr fullCalcOnLoad="1"', 1), 1)
    for after in ("<oleSize", "<customWorkbookViews", "<pivotCaches", "<extLst", "</workbook>"):
        i = wb_xml.find(after)
        if i >= 0:
            return wb_xml[:i] + '<calcPr fullCalcOnLoad="1"/>' + wb_xml[i:]
    return wb_xml


def set_cell(path: str, sheet: Sheet, row: int, col: int, value: str) -> None:
    """Write `value` into one cell, in place and atomically."""
    ext = os.path.splitext(path)[1].lower()
    if (row, col) in sheet.formulas:
        raise SheetError(f"{col_letters(col)}{row} contains a formula; I will not overwrite it")
    if not (1 <= row <= 1_048_576 and 1 <= col <= 16_384):
        raise SheetError("that cell is outside the sheet")
    if ext in (".csv", ".tsv"):
        _set_csv(path, row, col, value)
        return
    if ext == ".xlsm":
        raise SheetError("macro-enabled workbooks are read-only for safety")
    tmp = path + ".hearth-tmp"
    try:
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w") as zout:
            xml = _read_part(zin, sheet.part).decode("utf-8")
            patched = _patch_sheet_xml(xml, row, col, value, (row, col) in sheet.date_cells)
            for item in zin.infolist():
                data = zin.read(item)
                if item.filename == sheet.part:
                    data = patched.encode("utf-8")
                elif item.filename == "xl/workbook.xml" and "<f" in xml:
                    data = _force_recalc(data.decode("utf-8")).encode("utf-8")
                zout.writestr(item, data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _set_csv(path: str, row: int, col: int, value: str) -> None:
    text, bom = _csv_text(path)
    delim = _dialect(text, path)
    eol = "\r\n" if "\r\n" in text else "\n"
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delim))
    while len(rows) < row:
        rows.append([])
    rec = rows[row - 1]
    rec.extend([""] * (col - len(rec)))
    rec[col - 1] = value
    buf = io.StringIO(newline="")
    csv.writer(buf, delimiter=delim, lineterminator=eol).writerows(rows)
    out = bom + buf.getvalue()
    if not text.endswith(("\n", "\r")) and out.endswith(eol):
        out = out[: -len(eol)]
    tmp = path + ".hearth-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    os.replace(tmp, path)


# ---------------------------------------------------------------- writing a new workbook

_NUM = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?")


def _xml_text(v: str) -> str:
    v = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", v)
    return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def create_xlsx(path: str, book: list[tuple[str, list[list[str]]]]) -> None:
    """Write a new .xlsx: one worksheet per (name, rows). Numbers become numbers, everything else stays text; the first row is
    bold and the columns are as wide as their content. Written with the standard library only."""
    names: list[str] = []
    parts: list[str] = []
    for i, (raw, rows) in enumerate(book, start=1):
        name = re.sub(r"[\\/*?:\[\]]", " ", raw).strip()[:31] or f"Sheet{i}"
        while name.lower() in (n.lower() for n in names):
            name = (name[:28] + f" {i}")[:31]
        names.append(name)
        width: dict[int, int] = {}
        out: list[str] = []
        for r, row in enumerate(rows, start=1):
            cells = []
            for c, val in enumerate(row, start=1):
                val = "" if val is None else str(val)
                width[c] = max(width.get(c, 0), min(len(val), 60))
                ref = f"{col_letters(c)}{r}"
                style = ' s="1"' if r == 1 else ""
                if val == "":
                    continue
                if _NUM.fullmatch(val) and len(val) < 16:
                    cells.append(f'<c r="{ref}"{style}><v>{val}</v></c>')
                else:
                    cells.append(f'<c r="{ref}"{style} t="inlineStr"><is><t xml:space="preserve">{_xml_text(val)}</t></is></c>')
            out.append(f'<row r="{r}">{"".join(cells)}</row>')
        cols = "".join(f'<col min="{c}" max="{c}" width="{min(max(w + 2, 8), 62)}" customWidth="1"/>' for c, w in sorted(width.items()))
        parts.append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                     f'{"<cols>" + cols + "</cols>" if cols else ""}<sheetData>{"".join(out)}</sheetData></worksheet>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    content = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
               '<Default Extension="xml" ContentType="application/xml"/>'
               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
               '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
               + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                         for i in range(1, len(parts) + 1)) + "</Types>")
    workbook = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook {ns}><sheets>'
                + "".join(f'<sheet name="{_xml_text(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, start=1)) + "</sheets></workbook>")
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join(f'<Relationship Id="rId{i}" Type="{rel}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(parts) + 1))
               + f'<Relationship Id="rId{len(parts) + 1}" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    tmp = path + ".hearth-tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", content)
            z.writestr("_rels/.rels", root_rels)
            z.writestr("xl/workbook.xml", workbook)
            z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
            z.writestr("xl/styles.xml", styles)
            for i, part in enumerate(parts, start=1):
                z.writestr(f"xl/worksheets/sheet{i}.xml", part)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
