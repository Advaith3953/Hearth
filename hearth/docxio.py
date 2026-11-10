"""Word (.docx) text edits without extra packages. Only the matched text changes; formatting of everything else stays."""
from __future__ import annotations

import html
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

PART = "word/document.xml"
_P = re.compile(r"<w:p\b[^>]*>.*?</w:p>", re.S)
_T = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)


class DocxError(Exception):
    pass


def load(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo(PART)
            if info.file_size > 60_000_000:
                raise DocxError("the document is too large")
            return z.read(info).decode("utf-8")
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise DocxError("not a readable .docx file") from e


def save(path: str, xml: str) -> None:
    tmp = path + ".hearth-tmp"
    try:
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w") as zout:
            for item in zin.infolist():
                zout.writestr(item, xml.encode("utf-8") if item.filename == PART else zin.read(item))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def paragraph_texts(xml: str) -> list[str]:
    return ["".join(html.unescape(m.group(2)) for m in _T.finditer(p.group(0))) for p in _P.finditer(xml)]


def count(xml: str, old: str) -> int:
    return sum(t.count(old) for t in paragraph_texts(xml))


def replace(xml: str, old: str, new: str, replace_all: bool) -> tuple[str, int]:
    """Replace text inside paragraphs, even when Word split it across several formatting runs."""
    if not old:
        raise DocxError("nothing to look for")
    done = 0

    def fix(pm: re.Match[str]) -> str:
        nonlocal done
        para = pm.group(0)
        runs = list(_T.finditer(para))
        texts = [html.unescape(r.group(2)) for r in runs]
        full = "".join(texts)
        starts: list[int] = []
        i = full.find(old)
        while i >= 0 and (replace_all or done + len(starts) < 1):
            starts.append(i)
            i = full.find(old, i + len(old))
        if not starts:
            return para
        bounds = []
        pos = 0
        for t in texts:
            bounds.append((pos, pos + len(t)))
            pos += len(t)
        for s in reversed(starts):
            e = s + len(old)
            first = next(k for k, (a, b) in enumerate(bounds) if a <= s < b or (a == b == s))
            last = next(k for k, (a, b) in enumerate(bounds) if a < e <= b)
            head = texts[first][: s - bounds[first][0]]
            tail = texts[last][e - bounds[last][0]:]
            for k in range(first + 1, last + 1):
                texts[k] = ""
            texts[first] = head + new + (tail if first == last else "")
            if last != first:
                texts[last] = tail
        done += len(starts)
        it = iter(texts)

        def put(m: re.Match[str]) -> str:
            opening = m.group(1)
            if "xml:space" not in opening:
                opening = opening[:-1] + ' xml:space="preserve">'
            return opening + escape(next(it)) + m.group(3)
        return _T.sub(put, para)

    return _P.sub(fix, xml), done


def append(xml: str, text: str) -> str:
    paras = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(_CTRL.sub("", line))}</w:t></w:r></w:p>' for line in text.split("\n"))
    sect = xml.rfind("<w:sectPr")
    last_block = max(xml.rfind("</w:p>"), xml.rfind("</w:tbl>"))
    if sect > last_block >= 0:
        return xml[:sect] + paras + xml[sect:]
    end = xml.rfind("</w:body>")
    if end < 0:
        raise DocxError("unsupported document layout")
    return xml[:end] + paras + xml[end:]


_CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
       '<Default Extension="xml" ContentType="application/xml"/>'
       '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
_CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def create(path: str, paragraphs: list[str]) -> None:
    """Write a new, plain .docx (one paragraph per item; an empty item is an empty line)."""
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(_CTRL.sub("", p))}</w:t></w:r></w:p>' for p in paragraphs)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
           'w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>')
    tmp = path + ".hearth-tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", _CT)
            z.writestr("_rels/.rels", _RELS)
            z.writestr(PART, doc)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


@dataclass
class Run:
    """A stretch of text with one look. size is in points; color is 'RRGGBB' or None (automatic); url makes a link."""
    text: str
    bold: bool = False
    italic: bool = False
    size: float | None = None
    font: str | None = None
    color: str | None = None
    url: str | None = None
    underline: bool | None = None   # None: underlined only if it is a link


@dataclass
class Para:
    runs: list[Run] = field(default_factory=list)
    align: str = "left"          # left | center | right
    before: float = 0.0          # space above, points
    indent: float = 0.0          # left indent, points
    page_break: bool = False     # starts a new page
    hanging: float = 0.0         # first line sticks out to the left by this much (bullets)
    right_tab: bool = False      # a tab in this paragraph jumps to the right margin (dates on the right)
    line: float = 0.0            # exact distance between lines, points (0 = Word's default)
    border: tuple[str, float, float] | None = None   # a rule under the paragraph: (colour RRGGBB, thickness pt, gap pt)

    def text(self) -> str:
        return "".join(r.text for r in self.runs)


_REL_LINK = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')


def _run_xml(r: Run) -> str:
    props = ""
    if r.font:
        f = quoteattr(r.font)
        props += f"<w:rFonts w:ascii={f} w:hAnsi={f} w:cs={f}/>"
    props += "<w:b/>" if r.bold else ""
    props += "<w:i/>" if r.italic else ""
    color = r.color or ("0563C1" if r.url else None)
    props += f'<w:color w:val="{color}"/>' if color else ""
    if r.size:
        half = max(2, min(int(round(r.size * 2)), 400))
        props += f'<w:sz w:val="{half}"/><w:szCs w:val="{half}"/>'
    props += '<w:u w:val="single"/>' if (r.underline if r.underline is not None else bool(r.url)) else ""
    pieces = _CTRL.sub("", r.text).split("\t")
    body = "<w:tab/>".join(f'<w:t xml:space="preserve">{escape(t)}</w:t>' if t else "" for t in pieces)
    return f'<w:r>{f"<w:rPr>{props}</w:rPr>" if props else ""}{body}</w:r>'


def create_rich(path: str, paras: list[Para], page: tuple[float, float] = (595.0, 842.0),
                margins: tuple[float, float, float, float] = (72.0, 72.0, 72.0, 72.0)) -> int:
    """Write a new .docx that keeps fonts, sizes, bold/italic, colours, alignment, spacing, page breaks and hyperlinks.
    page = (width, height) and margins = (top, right, bottom, left), all in points. Returns the number of links written."""
    links: dict[str, str] = {}   # url -> relationship id
    text_width = page[0] - margins[1] - margins[3]
    body: list[str] = []
    for para in paras:
        ppr = ""
        ppr += "<w:pageBreakBefore/>" if para.page_break else ""
        if para.border:
            col, thick, gap = para.border
            ppr += (f'<w:pBdr><w:bottom w:val="single" w:sz="{max(2, int(round(thick * 8)))}" w:space="{max(0, min(int(round(gap)), 31))}" '
                    f'w:color="{col}"/></w:pBdr>')
        ppr += f'<w:tabs><w:tab w:val="right" w:pos="{int(text_width * 20)}"/></w:tabs>' if para.right_tab else ""
        line = f' w:line="{int(max(6.0, min(para.line, 400)) * 20)}" w:lineRule="exact"' if para.line else ""
        ppr += f'<w:spacing w:before="{int(max(0, min(para.before, 200)) * 20)}" w:after="0"{line}/>'
        hang = f' w:hanging="{int(min(para.hanging, para.indent) * 20)}"' if para.hanging >= 1 and para.indent >= 1 else ""
        ppr += f'<w:ind w:left="{int(max(0, min(para.indent, 600)) * 20)}"{hang}/>' if para.indent >= 1 else ""
        ppr += f'<w:jc w:val="{para.align}"/>' if para.align in ("center", "right") else ""
        out: list[str] = []
        i = 0
        while i < len(para.runs):                       # consecutive runs of one link share one <w:hyperlink>
            url = para.runs[i].url
            j = i
            while j < len(para.runs) and para.runs[j].url == url:
                j += 1
            xml = "".join(_run_xml(r) for r in para.runs[i:j])
            if url:
                rid = links.setdefault(url, f"rId{len(links) + 1}")
                xml = f'<w:hyperlink r:id="{rid}" w:history="1">{xml}</w:hyperlink>'
            out.append(xml)
            i = j
        body.append(f"<w:p><w:pPr>{ppr}</w:pPr>{''.join(out)}</w:p>")
    t, r_, b, l_ = (int(m * 20) for m in margins)
    pw, ph = int(page[0] * 20), int(page[1] * 20)
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_NS}><w:body>{"".join(body)}'
           f'<w:sectPr><w:pgSz w:w="{pw}" w:h="{ph}"/><w:pgMar w:top="{t}" w:right="{r_}" w:bottom="{b}" w:left="{l_}" '
           f'w:header="{min(360, t)}" w:footer="{min(360, b)}" w:gutter="0"/></w:sectPr></w:body></w:document>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="{rid}" Type="{_REL_LINK}" Target={quoteattr(url)} TargetMode="External"/>'
                      for url, rid in links.items()) + "</Relationships>")
    tmp = path + ".hearth-tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", _CT)
            z.writestr("_rels/.rels", _RELS)
            z.writestr(PART, doc)
            z.writestr("word/_rels/document.xml.rels", rels)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return len(links)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _on(el: ET.Element | None) -> bool | None:
    """A toggle like <w:b/> or <w:b w:val="0"/>: None when the element is absent."""
    if el is None:
        return None
    return el.get(_W + "val", "true").lower() not in ("0", "false", "off")


def _rpr(el: ET.Element | None) -> dict[str, object]:
    out: dict[str, object] = {}
    if el is None:
        return out
    for key, tag in (("bold", "b"), ("italic", "i")):
        v = _on(el.find(_W + tag))
        if v is not None:
            out[key] = v
    sz = el.find(_W + "sz")
    if sz is not None and sz.get(_W + "val", "").isdigit():
        out["size"] = int(sz.get(_W + "val", "0")) / 2
    col = el.find(_W + "color")
    if col is not None and re.fullmatch(r"[0-9A-Fa-f]{6}", col.get(_W + "val", "")):
        out["color"] = col.get(_W + "val", "").upper()
    rf = el.find(_W + "rFonts")
    if rf is not None and rf.get(_W + "ascii"):
        out["font"] = rf.get(_W + "ascii")
    return out


def _styles(z: zipfile.ZipFile) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """(document defaults, {style id: its own run and paragraph settings + 'based'}) from word/styles.xml, if there is one."""
    defaults: dict[str, object] = {}
    styles: dict[str, dict[str, object]] = {}
    try:
        root = ET.fromstring(z.read("word/styles.xml"))
    except (KeyError, ET.ParseError):
        return defaults, styles
    defaults = _rpr(root.find(f"{_W}docDefaults/{_W}rPrDefault/{_W}rPr"))
    for st in root.findall(_W + "style"):
        sid = st.get(_W + "styleId", "")
        entry: dict[str, object] = dict(_rpr(st.find(_W + "rPr")))
        based = st.find(_W + "basedOn")
        entry["based"] = based.get(_W + "val") if based is not None else None
        ppr = st.find(_W + "pPr")
        if ppr is not None:
            jc = ppr.find(_W + "jc")
            if jc is not None:
                entry["align"] = jc.get(_W + "val")
            sp = ppr.find(_W + "spacing")
            if sp is not None and sp.get(_W + "before", "").isdigit():
                entry["before"] = int(sp.get(_W + "before", "0")) / 20
            if ppr.find(_W + "numPr") is not None:
                entry["listed"] = True
        styles[sid] = entry
    return defaults, styles


def _style_chain(styles: dict[str, dict[str, object]], sid: str | None) -> dict[str, object]:
    chain: list[dict[str, object]] = []
    seen: set[str] = set()
    while sid and sid in styles and sid not in seen:
        seen.add(sid)
        chain.append(styles[sid])
        nxt = styles[sid].get("based")
        sid = nxt if isinstance(nxt, str) else None
    merged: dict[str, object] = {}
    for st in reversed(chain):
        merged.update({k: v for k, v in st.items() if k != "based"})
    return merged


def read_rich(path: str) -> tuple[list[Para], tuple[float, float], tuple[float, float, float, float]]:
    """A .docx as paragraphs with their looks (fonts, sizes, bold/italic, colours, alignment, indents, bullets, links, page
    breaks), page size and margins. Tables are flattened to one line per row; pictures are skipped. Used to make a PDF."""
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo(PART)
            if info.file_size > 60_000_000:
                raise DocxError("the document is too large")
            root = ET.fromstring(z.read(info))
            try:
                rels = {r.get("Id", ""): r.get("Target", "") for r in ET.fromstring(z.read("word/_rels/document.xml.rels"))
                        if r.get("TargetMode") == "External"}
            except (KeyError, ET.ParseError):
                rels = {}
            defaults, styles = _styles(z)
    except (zipfile.BadZipFile, KeyError, OSError, ET.ParseError) as e:
        raise DocxError("not a readable .docx file") from e
    body = root.find(_W + "body")
    if body is None:
        raise DocxError("the document has no body")
    page, margins = (595.0, 842.0), (72.0, 72.0, 72.0, 72.0)
    sect = body.find(_W + "sectPr")
    if sect is not None:
        sz, mar = sect.find(_W + "pgSz"), sect.find(_W + "pgMar")
        if sz is not None and sz.get(_W + "w", "").isdigit() and sz.get(_W + "h", "").isdigit():
            page = (int(sz.get(_W + "w", "0")) / 20, int(sz.get(_W + "h", "0")) / 20)
        if mar is not None:
            vals = [mar.get(_W + k, "") for k in ("top", "right", "bottom", "left")]
            if all(v.lstrip("-").isdigit() for v in vals):
                margins = tuple(max(0.0, int(v) / 20) for v in vals)   # type: ignore[assignment]
    paras: list[Para] = []

    def para(p: ET.Element) -> Para:
        ppr = p.find(_W + "pPr")
        pstyle = ppr.find(_W + "pStyle") if ppr is not None else None
        st = _style_chain(styles, pstyle.get(_W + "val") if pstyle is not None else None)
        out = Para()
        align = str(st.get("align") or "left")
        if ppr is not None:
            jc = ppr.find(_W + "jc")
            align = jc.get(_W + "val", align) if jc is not None else align
            ind = ppr.find(_W + "ind")
            if ind is not None:
                left = ind.get(_W + "left", ind.get(_W + "start", "0"))
                out.indent = int(left) / 20 if left.lstrip("-").isdigit() else 0.0
                hang = ind.get(_W + "hanging", "")
                out.hanging = int(hang) / 20 if hang.isdigit() else 0.0
            sp = ppr.find(_W + "spacing")
            if sp is not None and sp.get(_W + "before", "").isdigit():
                out.before = int(sp.get(_W + "before", "0")) / 20
            elif "before" in st:
                out.before = float(st["before"])  # type: ignore[arg-type]
            out.page_break = ppr.find(_W + "pageBreakBefore") is not None
            out.right_tab = any(t.get(_W + "val") == "right" for t in ppr.iter(_W + "tab"))
        if st.get("listed") or (ppr is not None and ppr.find(_W + "numPr") is not None):
            # a list item: the bullet itself lives in numbering.xml, so draw a dot
            out.indent = out.indent or 36.0
            out.hanging = out.hanging or 18.0
            out.runs.append(Run("\u2022\t"))
        out.align = "center" if align == "center" else "right" if align in ("right", "end") else "left"
        base = {**defaults, **st}

        def add(r: ET.Element, url: str | None) -> None:
            look = {**base, **_rpr(r.find(_W + "rPr"))}
            text = ""
            for node in r:
                if node.tag == _W + "t":
                    text += node.text or ""
                elif node.tag == _W + "tab":
                    text += "\t"
                elif node.tag in (_W + "br", _W + "cr"):
                    text += " "
            if text:
                out.runs.append(Run(text, bool(look.get("bold")), bool(look.get("italic")),
                                    float(look["size"]) if "size" in look else None,  # type: ignore[arg-type]
                                    str(look["font"]) if "font" in look else None,
                                    str(look["color"]) if "color" in look else None, url))

        for child in p:
            if child.tag == _W + "r":
                add(child, None)
            elif child.tag == _W + "hyperlink":
                url = rels.get(child.get(_RID, ""))
                for r in child.findall(_W + "r"):
                    add(r, url)
        if out.runs and out.runs[0].text == "\u2022\t" and len(out.runs) > 1:
            first = out.runs[1]
            out.runs[0] = Run("\u2022\t", False, False, first.size, first.font, first.color)
        return out

    for el in body:
        if el.tag == _W + "p":
            paras.append(para(el))
        elif el.tag == _W + "tbl":
            for tr in el.iter(_W + "tr"):
                cells = ["".join(t.text or "" for t in tc.iter(_W + "t")).strip() for tc in tr.findall(_W + "tc")]
                if any(cells):
                    paras.append(Para([Run("  |  ".join(c for c in cells if c))]))
    return paras, page, margins


def diff(old_xml: str, new_xml: str, limit: int = 8) -> str:
    a, b = paragraph_texts(old_xml), paragraph_texts(new_xml)
    lines: list[str] = []
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else ""
        y = b[i] if i < len(b) else ""
        if x != y:
            if x:
                lines.append(f"- {x[:200]}")
            if y:
                lines.append(f"+ {y[:200]}")
        if len(lines) >= limit * 2:
            lines.append("...")
            break
    return "\n".join(lines) or "(no visible change)"
