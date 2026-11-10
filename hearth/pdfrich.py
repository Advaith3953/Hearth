"""Copy a PDF's look into Word: fonts, sizes, bold/italic, colours, alignment, spacing, page breaks and hyperlinks.

A PDF has no real paragraphs or styles, only letters placed on a page, so this rebuilds them from the positions. It is a
best effort, not a pixel copy: images, boxes, backgrounds and real tables are not carried over (straight rules and underlines are), and a font
that is not on the computer is replaced by Word. Needs the optional package pdfminer.six (pip install pdfminer.six)."""
from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from typing import Any

from .docxio import Para, Run

_LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"}

MAX_PAGES = 200
_TRAILING_STYLE = re.compile(r"(semibold|demibold|bold|black|heavy|italic|oblique|light|medium)+$", re.I)
_SUBSET = re.compile(r"^[A-Z]{6}\+")
_SAME_AS = {"helvetica": "Arial", "arial": "Arial", "times": "Times New Roman", "timesnewroman": "Times New Roman",
            "courier": "Courier New", "couriernew": "Courier New", "symbol": "Symbol",
            # look-alike fonts with the same letter widths, which Word has: keeps line breaks and the page count
            "carlito": "Calibri", "caladea": "Cambria", "liberationsans": "Arial", "arimo": "Arial",
            "liberationserif": "Times New Roman", "tinos": "Times New Roman", "liberationmono": "Courier New",
            "cousine": "Courier New", "dejavusans": "Verdana", "opensans": "Segoe UI"}


class RichError(Exception):
    pass


def available() -> bool:
    return importlib.util.find_spec("pdfminer") is not None


@dataclass
class Rich:
    paras: list[Para]
    page: tuple[float, float]
    margins: tuple[float, float, float, float]   # top, right, bottom, left (points)
    links: int
    images: int
    pages: int


@dataclass
class _Line:
    x0: float
    y0: float
    x1: float
    y1: float
    runs: list[Run]
    size: float
    box: int
    box_x1: float
    bullet: float | None = None      # x of a drawn bullet dot just left of the line
    tail: list[Run] | None = None    # text on the same row at the right margin (a date), joined with a tab


def _clamp(v: float) -> float:
    return max(18.0, min(144.0, float(v)))


def font_style(fontname: str) -> tuple[str | None, bool, bool]:
    """'ABCDEF+Calibri-BoldItalic' -> ('Calibri', True, True). The family is None when it cannot be told."""
    name = _SUBSET.sub("", fontname or "")
    low = name.lower()
    bold = any(w in low for w in ("bold", "black", "heavy", "semibold", "demibold"))
    italic = "italic" in low or "oblique" in low
    family = re.split(r"[-,]", name, maxsplit=1)[0]
    family = re.sub(r"(PSMT|MT|PS)$", "", family)
    family = _TRAILING_STYLE.sub("", family)
    family = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", family).strip()
    mapped = _SAME_AS.get(re.sub(r"[^a-z]", "", family.lower()))
    return (mapped or family or None), bold, italic


def to_hex(color: Any) -> str | None:
    """A PDF colour (grey, RGB or CMYK) as RRGGBB; black and near-white give None (automatic), because the page
    background is not copied and white text would vanish."""
    if color is None:
        return None
    try:
        vals = [float(color)] if isinstance(color, (int, float)) else [float(x) for x in color]
    except (TypeError, ValueError):
        return None
    if len(vals) == 1:
        r = g = b = vals[0]
    elif len(vals) == 3:
        r, g, b = vals
    elif len(vals) == 4:
        c, m, y, k = vals
        r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
    else:
        return None
    rgb = [max(0, min(255, round(v * 255))) for v in (r, g, b)]
    if max(rgb) < 12 or min(rgb) > 235:
        return None
    return "".join(f"{v:02X}" for v in rgb)


def _links(page: Any) -> list[tuple[float, float, float, float, str]]:
    from pdfminer.pdftypes import resolve1
    out: list[tuple[float, float, float, float, str]] = []
    for ref in resolve1(page.annots) or []:
        try:
            annot = resolve1(ref)
            action = resolve1(annot.get("A"))
            uri = resolve1(action.get("URI")) if action else None
            rect = [float(v) for v in resolve1(annot["Rect"])]
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if isinstance(uri, bytes):
            uri = uri.decode("utf-8", "replace")
        if isinstance(uri, str) and uri.strip() and len(rect) == 4:
            out.append((min(rect[0], rect[2]), min(rect[1], rect[3]), max(rect[0], rect[2]), max(rect[1], rect[3]), uri.strip()))
    return out


def _count_images(el: Any) -> int:
    from pdfminer.layout import LTFigure, LTImage
    n = 0
    for child in el:
        if isinstance(child, LTImage):
            n += 1
        elif isinstance(child, LTFigure):
            n += _count_images(child)
    return n


_URL = re.compile(r"(?:https?://|www\.)[^\s|<>\"]+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I)
_DOMAIN = re.compile(r"^[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?:/\S*)?$", re.I)


def _linkify(runs: list[Run]) -> list[Run]:
    """Many PDFs (Chrome, Word) have underlined web addresses with no link inside them. Whatever looks like a web address
    or an e-mail address becomes a real link; a bare domain needs the underline to count."""
    out: list[Run] = []
    for r in runs:
        if r.url:
            out.append(r)
            continue
        whole = r.text.strip().rstrip(".,;:)")
        if r.underline and "@" not in whole and _DOMAIN.match(whole):
            out.append(Run(r.text, r.bold, r.italic, r.size, r.font, r.color, "https://" + whole, True))
            continue
        pos = 0
        for m in _URL.finditer(r.text):
            hit = m.group(0).rstrip(".,;:)")
            if hit.lower().startswith(("http", "www.")) or "@" not in hit:
                target = hit if hit.lower().startswith("http") else "https://" + hit
            else:
                target = "mailto:" + hit
            if m.start() > pos:
                out.append(Run(r.text[pos:m.start()], r.bold, r.italic, r.size, r.font, r.color, None, r.underline))
            out.append(Run(hit, r.bold, r.italic, r.size, r.font, r.color, target, r.underline))
            pos = m.start() + len(hit)
        if pos == 0:
            out.append(r)
        elif pos < len(r.text):
            out.append(Run(r.text[pos:], r.bold, r.italic, r.size, r.font, r.color, None, r.underline))
    return out


def _thin_shapes(layout: Any) -> list[tuple[float, float, float, float, str]]:
    """Drawn straight rules (borders under headings, underlines): (x0, y0, x1, y1, colour) of very flat or very narrow shapes."""
    from pdfminer.layout import LTCurve, LTFigure, LTLine, LTRect
    out: list[tuple[float, float, float, float, str]] = []
    for el in layout:
        if isinstance(el, LTFigure):
            out += _thin_shapes(el)
        elif isinstance(el, (LTRect, LTLine, LTCurve)):
            w, h = el.x1 - el.x0, el.y1 - el.y0
            if h <= 2.5 and w >= 12:
                filled = bool(getattr(el, "fill", False))
                main = getattr(el, "non_stroking_color", None) if filled else getattr(el, "stroking_color", None)
                col = to_hex(main) or ("000000" if main is not None else None) or "000000"
                out.append((el.x0, el.y0, el.x1, max(el.y1, el.y0 + 0.5), col))
    return out


def _line(line: Any, box: int, box_x1: float, links: list[tuple[float, float, float, float, str]],
          rules: list[tuple[float, float, float, float, str]] | None = None) -> _Line | None:
    from pdfminer.layout import LTChar
    runs: list[Run] = []
    key: tuple[object, ...] | None = None
    sizes: list[float] = []
    for ch in line:
        if isinstance(ch, LTChar):
            cx, cy = (ch.x0 + ch.x1) / 2, (ch.y0 + ch.y1) / 2
            url = next((u for x0, y0, x1, y1, u in links if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1), None)
            family, bold, italic = font_style(ch.fontname)
            size = round(float(ch.size), 1)
            color = to_hex(getattr(ch.graphicstate, "ncolor", None))
            under = any(x0 - 1 <= cx <= x1 + 1 and ch.y0 - 2.5 <= y1 and y0 <= ch.y0 + 1.5 and (x1 - x0) <= (line.x1 - line.x0) + 3
                        for x0, y0, x1, y1, _ in (rules or []))
            k = (family, bold, italic, size, color, url, under)
            if k != key or not runs:
                runs.append(Run("", bold, italic, size, family, color, url, under))
                key = k
            runs[-1].text += _LIGATURES.get(ch.get_text(), ch.get_text())
            sizes.append(size)
        else:
            text = ch.get_text()
            if runs and text.strip() == "" and "\n" not in text:
                runs[-1].text += text
    if not runs or not "".join(r.text for r in runs).strip():
        return None
    runs[-1].text = runs[-1].text.rstrip()
    runs[0].text = runs[0].text.lstrip()
    runs = _linkify(runs)
    return _Line(line.x0, line.y0, line.x1, line.y1, runs, max(sizes), box, box_x1)


def _mark_bullets(lines: list[_Line], shapes: list[Any]) -> None:
    """Chrome and Word draw bullet dots as tiny shapes, not letters. A small shape just left of a line is its bullet."""
    dots = [s for s in shapes if 0 < s.width <= 8 and 0 < s.height <= 8]
    for ln in lines:
        for d in dots:
            if 0 <= ln.x0 - d.x1 <= 24 and ln.y0 - 3 <= (d.y0 + d.y1) / 2 <= ln.y1 + 3:
                ln.bullet = d.x0
                break


def _attach_tails(lines: list[_Line], left: float, width: float) -> list[_Line]:
    """A short piece on the same row, flush with the right margin (a date), joins that row's line behind a tab."""
    kept: list[_Line] = []
    for ln in lines:
        host = next((h for h in kept if h.tail is None and abs(h.y0 - ln.y0) < 0.35 * max(h.size, ln.size)
                     and ln.x0 > h.x1 + 12 and ln.x1 > left + width * 0.97 and ln.x0 > left + width * 0.5), None)
        if host is not None:
            host.tail = ln.runs
        else:
            kept.append(ln)
    return kept


def _pitch(group: list[_Line], size: float) -> float:
    """Distance between the lines of a paragraph: the average from where the lines actually sit (one line: 1.2 times the size)."""
    if len(group) > 1:
        return round((group[0].y0 - group[-1].y0) / (len(group) - 1), 2)
    return round(1.2 * size, 2)


def _rule_under(ln: _Line, left: float, width: float,
                shapes: list[tuple[float, float, float, float, str]]) -> tuple[str, float, float] | None:
    """A drawn line across the page right under this line of text becomes the paragraph's bottom border."""
    best: tuple[float, tuple[str, float, float]] | None = None
    for x0, y0, x1, y1, col in shapes:
        if x1 - x0 < 0.6 * width or x0 > left + 0.2 * width:
            continue
        dist = ln.y0 - y1                      # from the bottom of the letters down to the rule
        if -3 <= dist <= 14 and (best is None or dist < best[0]):
            best = (dist, (col, round(max(0.5, y1 - y0), 1), round(max(0.0, dist), 1)))
    return best[1] if best else None


def _paragraphs(lines: list[_Line], left: float, width: float,
                shapes: list[tuple[float, float, float, float, str]] | None = None) -> list[Para]:
    """Group wrapped lines into paragraphs, then work out alignment, indent, bullets and the space above each one."""
    lines = _attach_tails(lines, left, width)
    groups: list[list[_Line]] = []
    for ln in lines:
        prev = groups[-1][-1] if groups else None
        wraps = (prev is not None and ln.bullet is None and ln.box == prev.box and abs(ln.size - prev.size) < 0.6
                 and abs(ln.x0 - prev.x0) < 3 and prev.tail is None and prev.x1 >= left + width - 6 * prev.size
                 and 0 < prev.y0 - ln.y0 < 1.7 * ln.size)
        if wraps:
            groups[-1].append(ln)
        else:
            groups.append([ln])
    paras: list[Para] = []
    last_rule = 0.0
    model_y: float | None = None
    for g in groups:
        first = g[0]
        runs = [Run(r.text, r.bold, r.italic, r.size, r.font, r.color, r.url, r.underline) for r in first.runs]
        if first.bullet is not None:
            runs.insert(0, Run("\u2022\t", False, False, first.runs[0].size, first.runs[0].font, first.runs[0].color))
        for nxt in g[1:]:
            runs.append(Run(" ", runs[-1].bold, runs[-1].italic, runs[-1].size, runs[-1].font))
            runs += [Run(r.text, r.bold, r.italic, r.size, r.font, r.color, r.url, r.underline) for r in nxt.runs]
        tail = g[-1].tail
        if tail:
            runs.append(Run("\t", False, False, runs[-1].size, runs[-1].font))
            runs += [Run(r.text, r.bold, r.italic, r.size, r.font, r.color, r.url, r.underline) for r in tail]
        indent, align, hanging = max(0.0, first.x0 - left), "left", 0.0
        if first.bullet is not None:
            hanging = max(6.0, first.x0 - first.bullet)
        elif len(g) == 1 and not tail:
            centre = (first.x0 + first.x1) / 2
            if indent > 0.1 * width and abs(centre - (left + width / 2)) < 0.03 * width:
                align, indent = "center", 0.0
            elif first.x1 > left + width * 0.98 and indent > 0.3 * width:
                align, indent = "right", 0.0
        pitch = _pitch(g, first.size)
        border = _rule_under(g[-1], left, width, shapes or [])
        # Work out the space above from where Word will really put the previous line, so rounding never piles up
        before = 0.0 if model_y is None else max(0.0, round((model_y - last_rule - pitch - first.y0) * 20) / 20)
        before = before if before > 0.4 else 0.0
        base = first.y0 if model_y is None else model_y - last_rule - pitch - before
        model_y = base - pitch * (len(g) - 1)
        para = Para(runs, align, before, indent, hanging=hanging, right_tab=bool(tail),
                    line=pitch, border=border)
        last_rule = (border[1] + border[2]) if border else 0.0
        paras.append(para)
    return paras


def extract(path: str, pages: list[int]) -> Rich:
    """The chosen pages (1-based, ascending) of a PDF as Word paragraphs."""
    if not available():
        raise RichError("keeping the formatting needs the pdfminer.six package: run  python -m pip install pdfminer.six  and ask again")
    if len(pages) > MAX_PAGES:
        raise RichError(f"formatted copy is limited to {MAX_PAGES} pages at a time")
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTCurve, LTTextContainer
    from pdfminer.pdfpage import PDFPage
    wanted = sorted(set(pages))
    out: list[Para] = []
    links_seen: set[str] = set()
    images = 0
    page_size = (595.0, 842.0)
    margins = (72.0, 72.0, 72.0, 72.0)
    try:
        with open(path, "rb") as fh:
            annots = [_links(p) for p in PDFPage.get_pages(fh, pagenos={n - 1 for n in wanted})]
        for idx, layout in enumerate(extract_pages(path, page_numbers=[n - 1 for n in wanted], laparams=LAParams())):
            lines: list[_Line] = []
            shapes = _thin_shapes(layout)
            for b, box in enumerate(el for el in layout if isinstance(el, LTTextContainer)):
                for ln in box:
                    got = _line(ln, b, box.x1, annots[idx] if idx < len(annots) else [], shapes)
                    if got:
                        lines.append(got)
            images += _count_images(layout)
            _mark_bullets(lines, [el for el in layout if isinstance(el, LTCurve)])
            if idx == 0:
                page_size = (float(layout.width), float(layout.height))
                if lines:
                    top_line = max(lines, key=lambda x: x.y1)
                    lead = max(0.0, 1.2 * top_line.size - top_line.size)       # exact line height adds this above the letters
                    margins = (max(10.0, _clamp(layout.height - top_line.y1) - lead), _clamp(layout.width - max(x.x1 for x in lines)),
                               max(4.0, min(144.0, min(x.y0 for x in lines) - 12)), _clamp(min(x.x0 for x in lines)))
            left = margins[3]
            width = max(50.0, float(layout.width) - margins[3] - margins[1])
            paras = _paragraphs(lines, left, width, shapes)
            for p in paras:
                links_seen.update(r.url for r in p.runs if r.url)
            if paras and idx > 0:
                paras[0].page_break = True
            out += paras
    except RichError:
        raise
    except Exception as e:  # noqa: BLE001  pdfminer raises many kinds of errors for damaged or protected files
        raise RichError(f"could not read the layout of this PDF ({type(e).__name__})") from e
    if not out:
        raise RichError("no text with a layout was found (a scanned PDF has only pictures)")
    return Rich(out, page_size, margins, len(links_seen), images, len(wanted))
