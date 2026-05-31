"""PDF → per-page markdown with `## Page N` citations + structured pages.

Adapts markitdown's PdfConverter but ALWAYS emits per-page chunks: the
upstream converter collapses to a single pdfminer blob when most pages are
prose, destroying the page boundaries RAG needs for citation. Adds
decoration cropping, multi-column/glossary layout routing, font-size
heading detection, running header/footer stripping, and `(cid:NNN)` /
hyphen-break cleanup — each tuned against a 1243-PDF benchmark.

Runs in the SIGKILL-able worker (`isolation = True`). Raises
`EncryptedPdfError` for password-protected files and `NeedsOcrError` when
text density is too low (scanned/image-only).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

from ..config import settings
from ..exceptions import EncryptedPdfError, NeedsOcrError
from ..metadata import clean, parse_pdf_date
from .base import BaseParser, ParseResult

log = logging.getLogger("parser.pdf")

# Per-page raw-text density below which the PDF is declared image-only.
_OCR_DENSITY_THRESHOLD = settings.ocr_density_threshold

# `(cid:NNN)` — non-ToUnicode font fallback pdfminer emits when it can't
# decode a character. Common on math-heavy LaTeX PDFs; pure noise for RAG.
_RE_CID_ARTIFACT = re.compile(r"\(cid:\d+\)")
# A line that is *entirely* char-codes — `(cid:17)(cid:18)` or `/66 /33 /i255`.
# Unambiguous broken-text-layer signal (a real line is never all codes), so
# this avoids false positives on dates/fractions that bare `/\d+` would hit.
_RE_CODE_ONLY_LINE = re.compile(r"^(?:\(cid:\d+\)\s*)+$|^(?:/i?\d+\s*)+$")
# Hyphenated line breaks: `compu-\ntation` -> `computation`. Conservative:
# only join lowercase-lowercase so intentional hyphens survive.
_RE_HYPHEN_BREAK = re.compile(r"([a-z])-\n([a-z])")
# Excess vertical whitespace from per-page concatenation / stripped lines.
_RE_EXCESS_BLANK = re.compile(r"\n{3,}")

# Page-decoration filter constants. Many legal/textbook PDFs decorate pages
# with vertical chapter labels in the right margin and banner art atop
# page 1; pdfminer reads these as garbage mixed into body content. 30px
# right-margin + 140px page-1 header crops hold across the HKEX corpus
# without clipping real body text.
_PDF_SIDEBAR_MARGIN = 30
_PDF_HEADER_Y_MAX_PAGE_1 = 140


def _preprocess_page(page: Any, page_number: int) -> Any:
    """Return a cropped view of `page` with decoration zones excluded.

    Crops the right-margin vertical chapter labels (30px strip) and, on
    page 1 only, the top banner art (140px). All downstream extraction sees
    this cropped view. Falls back to the original page if crop raises.
    """
    sidebar_x = float(page.width) - _PDF_SIDEBAR_MARGIN
    header_y = float(_PDF_HEADER_Y_MAX_PAGE_1) if page_number == 1 else 0.0
    bbox = (0.0, header_y, sidebar_x, float(page.height))
    try:
        return page.crop(bbox, relative=False)
    except Exception:
        log.debug("page %d: crop failed, using uncropped page", page_number, exc_info=True)
        return page


def _classify_page_layout(page: Any) -> tuple[str, float | None]:
    """Classify a page as 'single', 'prose_multi', or 'glossary'.

    Returns `(kind, divider_x)` (divider None for 'single'). Heuristic from
    word positions:
      A) middle_starts: lines starting in the 35-60% band (signals a 2nd col)
      B) right_only: lines entirely on the right half (glossary continuations)
      C) left_only: lines entirely on the left half

    'glossary' is checked before 'prose_multi' because a glossary has a 2nd
    column but no independent column-prose — per-column extraction would
    unstitch term↔definition pairs.
    """
    try:
        words = page.extract_words(x_tolerance=2, y_tolerance=2)
    except Exception:
        log.debug("layout classify: extract_words failed, assuming single column", exc_info=True)
        return ("single", None)
    if len(words) < 30:
        return ("single", None)

    page_width = float(page.width)
    midpoint = page_width / 2

    lines: dict[int, dict] = {}
    for w in words:
        y_key = int(w["top"] / 4) * 4
        entry = lines.setdefault(y_key, {"x_min": w["x0"], "has_left": False, "has_right": False})
        if w["x0"] < entry["x_min"]:
            entry["x_min"] = w["x0"]
        if w["x0"] < midpoint:
            entry["has_left"] = True
        else:
            entry["has_right"] = True
    if len(lines) < 8:
        return ("single", None)

    middle_band_lo = page_width * 0.35
    middle_band_hi = page_width * 0.60
    middle_starts = [
        e["x_min"] for e in lines.values() if middle_band_lo < e["x_min"] < middle_band_hi
    ]
    n_lines = len(lines)
    n_middle = len(middle_starts)
    n_right_only = sum(1 for e in lines.values() if e["has_right"] and not e["has_left"])
    n_left_only = sum(1 for e in lines.values() if e["has_left"] and not e["has_right"])

    if n_middle < n_lines * 0.15:
        return ("single", None)

    middle_starts.sort()
    divider = middle_starts[len(middle_starts) // 2]

    right_only_ratio = n_right_only / n_lines
    left_only_ratio = n_left_only / n_lines

    # Glossary: right column has standalone continuation lines, left doesn't.
    if right_only_ratio >= 0.15 and left_only_ratio < 0.05:
        return ("glossary", divider)

    # Prose-multi (NeurIPS-style): both columns independent AND a clean
    # gutter. The gutter check rejects math-heavy pages with scattered word
    # X-positions that would otherwise be falsely column-split.
    gutter_band = 10.0
    n_straddlers = sum(1 for w in words if abs((w["x0"] + w["x1"]) / 2 - divider) < gutter_band)
    straddler_ratio = n_straddlers / len(words)

    if left_only_ratio >= 0.10 and right_only_ratio >= 0.10 and straddler_ratio < 0.05:
        return ("prose_multi", divider)

    return ("single", None)


def _strict_form_detect(page: Any) -> str | None:
    """Wrap vendored form-detection with a false-positive filter.

    The vendored heuristic flags any page where ≥20% of rows are
    column-aligned — but title/author blocks and column-aligned math hit
    that too, producing garbage pipe-tables. Real tables have ≥3
    *consecutive* data rows; require that before accepting the table.
    """
    from ..pdf_helpers import extract_form_content_from_words

    result = extract_form_content_from_words(page)
    if result is None:
        return None
    cur, longest = 0, 0
    for line in result.split("\n"):
        s = line.strip()
        if s.startswith("|") and "---" not in s:
            cur += 1
            if cur > longest:
                longest = cur
        else:
            cur = 0
    return result if longest >= 3 else None


def _detect_body_font_size(pdf: Any) -> float:
    """Median font size across (up to) the first 20 pages. Body text rarely
    changes size across a document, so the first chunk is representative and
    avoids tripling extraction cost on long papers."""
    sizes: list[float] = []
    for page in pdf.pages[:20]:
        try:
            words = page.extract_words(extra_attrs=["size"])
        except Exception:
            log.debug("body-font sampling: extract_words failed on a page", exc_info=True)
            continue
        sizes.extend(w["size"] for w in words if "size" in w)
    if not sizes:
        return 10.0
    sizes.sort()
    return sizes[len(sizes) // 2]


def _extract_prose_in_x_range(page: Any, x_min: float, x_max: float, body_size: float) -> str:
    """Extract prose from a vertical strip [x_min, x_max) of a page.

    Replaces `page.crop` for column extraction — crop's bbox semantics let
    right-column first letters bleed into the left column. We filter words
    by center X, then run the same heading-aware line assembly as
    `_extract_prose_with_headings`.
    """
    try:
        words = page.extract_words(
            extra_attrs=["size"],
            keep_blank_chars=True,
            x_tolerance=3,
            y_tolerance=3,
        )
    except Exception:
        log.debug("column extract: extract_words failed, dropping column text", exc_info=True)
        return ""

    col_words = [w for w in words if x_min <= (w["x0"] + w["x1"]) / 2 < x_max and "size" in w]
    if not col_words:
        return ""

    lines: dict[int, list[dict]] = {}
    for w in col_words:
        y_key = int(round(w["top"] / 3) * 3)
        lines.setdefault(y_key, []).append(w)

    parts: list[str] = []
    for y in sorted(lines):
        line_words = sorted(lines[y], key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in line_words).strip()
        if not text:
            continue
        avg_size = sum(w["size"] for w in line_words) / len(line_words)
        ratio = avg_size / body_size if body_size > 0 else 1.0
        is_heading_shape = (
            4 <= len(text) <= 150
            and "|" not in text
            and not text.replace(".", "").replace(" ", "").isdigit()
        )
        if is_heading_shape:
            if ratio >= 1.50:
                parts.append(f"# {text}")
                continue
            if ratio >= 1.25:
                parts.append(f"## {text}")
                continue
        parts.append(text)
    return "\n".join(parts)


def _extract_prose_with_headings(page: Any, body_size: float) -> str:
    """Extract text emitting `#`/`##` for heading-sized lines.

    Conservative (precision over recall — false-positive headings hurt RAG
    more than missing real ones): only lines 4-150 chars, no pipe char, not
    all-numeric, qualify; ratio ≥1.50× body -> `#`, ≥1.25× -> `##`. The
    `###` tier is intentionally dropped (noisiest). Falls back to
    `extract_text()` if word extraction fails.
    """
    try:
        words = page.extract_words(
            extra_attrs=["size"],
            keep_blank_chars=True,
            x_tolerance=3,
            y_tolerance=3,
        )
    except Exception:
        log.debug(
            "prose extract: extract_words failed, falling back to extract_text", exc_info=True
        )
        return (page.extract_text() or "").strip()

    if not words:
        return ""

    lines: dict[int, list[dict]] = {}
    for w in words:
        if "size" not in w:
            continue
        y_key = int(round(w["top"] / 3) * 3)
        lines.setdefault(y_key, []).append(w)

    if not lines:
        return (page.extract_text() or "").strip()

    parts: list[str] = []
    for y in sorted(lines.keys()):
        line_words = sorted(lines[y], key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in line_words).strip()
        if not text:
            continue
        avg_size = sum(w["size"] for w in line_words) / len(line_words)
        ratio = avg_size / body_size if body_size > 0 else 1.0
        is_heading_shape = (
            4 <= len(text) <= 150
            and "|" not in text
            and not text.replace(".", "").replace(" ", "").isdigit()
        )
        if is_heading_shape:
            if ratio >= 1.50:
                parts.append(f"# {text}")
                continue
            if ratio >= 1.25:
                parts.append(f"## {text}")
                continue
        parts.append(text)
    return "\n".join(parts)


def _strip_repeated_running_lines(per_page_texts: list[str], min_repetitions: int = 3) -> list[str]:
    """Strip lines appearing at top/bottom of N+ pages — running
    headers/footers (journal name, paper title, "Page N of M", copyright).
    Pure noise for RAG. Only strips lines <200 chars appearing ≥max(3,
    n_pages/3) times."""
    if len(per_page_texts) < min_repetitions:
        return per_page_texts

    first_lines: Counter[str] = Counter()
    last_lines: Counter[str] = Counter()
    for pt in per_page_texts:
        lines = [line.strip() for line in pt.split("\n") if line.strip()]
        if not lines:
            continue
        first_lines[lines[0]] += 1
        last_lines[lines[-1]] += 1

    threshold = max(min_repetitions, len(per_page_texts) // 3)
    repeated: set[str] = set()
    for line, count in first_lines.items():
        if count >= threshold and len(line) < 200:
            repeated.add(line)
    for line, count in last_lines.items():
        if count >= threshold and len(line) < 200:
            repeated.add(line)
    if not repeated:
        return per_page_texts

    cleaned: list[str] = []
    for pt in per_page_texts:
        lines = pt.split("\n")
        while lines and lines[0].strip() in repeated:
            lines.pop(0)
        while lines and lines[-1].strip() in repeated:
            lines.pop()
        cleaned.append("\n".join(lines))
    return cleaned


def _post_clean(text: str) -> str:
    """Per-page cleanup. Order matters: cid stripping before hyphen joining
    (cid artifacts at the join site would block the merge), whitespace last."""
    text = _RE_CID_ARTIFACT.sub("", text)
    text = _RE_HYPHEN_BREAK.sub(r"\1\2", text)
    text = _RE_EXCESS_BLANK.sub("\n\n", text)
    return text


def _font_garbage_ratio(raw_pages: list[str]) -> float:
    """Fraction of the extracted text that is CID/char-code garbage.

    Computed on the RAW per-page text (before `_post_clean` strips `(cid:..)`),
    because stripping the codes would hide a broken text layer. We count
    `(cid:NN)` references and *code-only lines* (a normal line is never made
    entirely of `/66 /33` codes), so dates/fractions like `12/25` don't
    trigger it. Returns max(cid_ratio, code_line_ratio). Adapted from
    hybrid_parser's has_font_encoding_issues.
    """
    text = "\n".join(raw_pages)
    if len(text.strip()) < 50:
        return 0.0
    words = text.split()
    cid_ratio = len(_RE_CID_ARTIFACT.findall(text)) / max(1, len(words))
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    code_lines = sum(1 for ln in lines if _RE_CODE_ONLY_LINE.match(ln))
    line_ratio = code_lines / max(1, len(lines))
    return max(cid_ratio, line_ratio)


def _extract_per_page(input_path: Path) -> tuple[list[str], dict]:
    """Extract one cleaned markdown string per page.

    Returns `(per_page, info)` where `info` carries routing signals:
    `font_garbage_ratio`, `bytes_per_page`, and the PDF `creator` metadata.
    Raises EncryptedPdfError for password-protected files.
    """
    import pdfplumber

    # pdfplumber 0.11 rewraps pdfminer's encryption errors in its own
    # PdfminerException and drops the message — catch the wrapper and walk
    # __cause__/__context__ to recover the real cause.
    from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect
    from pdfplumber.utils.exceptions import PdfminerException

    def _is_password_error(exc: BaseException) -> bool:
        seen: set[int] = set()
        cur: BaseException | None = exc
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            if isinstance(cur, (PDFPasswordIncorrect, PDFEncryptionError)):
                return True
            cur = cur.__cause__ or cur.__context__
        return False

    try:
        pdf = pdfplumber.open(str(input_path))
    except (PDFPasswordIncorrect, PDFEncryptionError) as exc:
        raise EncryptedPdfError(str(exc) or "PDF requires a password") from exc
    except PdfminerException as exc:
        if _is_password_error(exc):
            raise EncryptedPdfError("PDF requires a password") from exc
        raise

    try:
        try:
            pages = pdf.pages  # touch lazily — same auth check
        except (PDFPasswordIncorrect, PDFEncryptionError) as exc:
            raise EncryptedPdfError(str(exc) or "PDF requires a password") from exc
        except PdfminerException as exc:
            if _is_password_error(exc):
                raise EncryptedPdfError("PDF requires a password") from exc
            raise

        body_size = _detect_body_font_size(pdf)
        meta = pdf.metadata or {}
        creator = f"{meta.get('Producer', '')} {meta.get('Creator', '')}".strip()
        doc_props = {
            "title": (meta.get("Title") or "").strip(),
            "author": (meta.get("Author") or "").strip(),
            "created": parse_pdf_date(meta.get("CreationDate")),
            "modified": parse_pdf_date(meta.get("ModDate")),
        }

        per_page: list[str] = []
        raw_pages: list[str] = []  # pre-clean text, for the font-garbage check
        for page_idx, raw_page in enumerate(pages, start=1):
            page = _preprocess_page(raw_page, page_idx)
            layout, divider = _classify_page_layout(page)

            if layout == "prose_multi" and divider is not None:
                col_chunks: list[str] = []
                for x0, x1 in [(0.0, divider), (divider, float(page.width))]:
                    col_text = _extract_prose_in_x_range(page, x0, x1, body_size)
                    if col_text.strip():
                        col_chunks.append(col_text.strip())
                page_md = "\n\n".join(col_chunks)
            else:
                form_md = _strict_form_detect(page)
                if form_md is not None:
                    page_md = form_md
                else:
                    page_md = _extract_prose_with_headings(page, body_size)

            raw_pages.append(page_md)
            per_page.append(_post_clean(page_md).strip())
    finally:
        pdf.close()

    try:
        size = input_path.stat().st_size
    except OSError:
        size = 0
    info = {
        "font_garbage_ratio": _font_garbage_ratio(raw_pages),
        "bytes_per_page": size / max(1, len(per_page)),
        "creator": creator,
        "doc_props": doc_props,
    }
    return _strip_repeated_running_lines(per_page), info


def _join_pages(per_page: list[str]) -> str:
    """Join pages with `## Page N` boundaries, then MasterFormat stitching."""
    from ..pdf_helpers import merge_partial_numbering_lines

    parts: list[str] = []
    for i, page_md in enumerate(per_page, start=1):
        if page_md:
            parts.append(f"## Page {i}\n\n{page_md}")
        else:
            parts.append(f"## Page {i}")
    md = "\n\n".join(parts)
    md = merge_partial_numbering_lines(md)
    return _RE_EXCESS_BLANK.sub("\n\n", md)


def _check_font_encoding(info: dict) -> None:
    """Raise NeedsOcrError if the text layer is mostly CID/char-code garbage.

    Runs BEFORE the density check and before `_post_clean` stripping has any
    say — a partially broken font layer (real text + many `(cid:NN)`) would
    otherwise be silently stripped to incomplete text and pass density.
    """
    ratio = info.get("font_garbage_ratio", 0.0)
    if ratio > settings.pdf_font_garbage_threshold:
        raise NeedsOcrError(
            f"text layer is {ratio:.0%} CID/char-code garbage "
            f"(> {settings.pdf_font_garbage_threshold:.0%} threshold) — broken "
            f"font Unicode maps; glyphs render but don't extract, needs OCR"
        )


def _check_ocr_density(md: str, page_count: int, info: dict) -> None:
    """Raise NeedsOcrError if body text is too sparse (scanned/image-only).

    Two triggers:
      1. avg chars/page below the hard floor (`ocr_density_threshold`).
      2. a heavy file (bytes/page over `pdf_scanned_bytes_per_page`) whose text
         is merely thin (< 3× the floor) — almost certainly scanned with a
         poor text layer, even though it cleared trigger 1. A strong digital
         creator signature (Word/LaTeX/Acrobat/…) suppresses trigger 2, since
         a digital doc can legitimately be image-heavy-but-sparse.
    """
    body_chars = len(md) - sum(len(s) for s in re.findall(r"## Page \d+", md))
    page_count = max(1, page_count)
    chars_per_page = body_chars / page_count

    if chars_per_page < _OCR_DENSITY_THRESHOLD:
        raise NeedsOcrError(
            f"only {body_chars} text chars across {page_count} pages "
            f"({chars_per_page:.0f} chars/page < threshold "
            f"{_OCR_DENSITY_THRESHOLD}) — PDF likely scanned/image-only "
            f"(creator={info.get('creator') or 'unknown'!r})"
        )

    creator = (info.get("creator") or "").lower()
    digital = any(t in creator for t in ("word", "acrobat", "pdfmaker", "latex", "libreoffice"))
    bytes_per_page = info.get("bytes_per_page", 0)
    if (
        not digital
        and bytes_per_page > settings.pdf_scanned_bytes_per_page
        and chars_per_page < _OCR_DENSITY_THRESHOLD * 3
    ):
        raise NeedsOcrError(
            f"heavy file ({bytes_per_page / 1024:.0f} KB/page > "
            f"{settings.pdf_scanned_bytes_per_page / 1024:.0f} KB) with thin text "
            f"({chars_per_page:.0f} chars/page) and no digital creator signature "
            f"— likely scanned with a poor text layer"
        )


class PdfParser(BaseParser):
    name = "markitdown-pdf+pages"
    extensions = (".pdf",)
    isolation = True

    def parse(self, path: Path, mode: str) -> ParseResult:
        per_page, info = _extract_per_page(path)
        _check_font_encoding(info)
        md = _join_pages(per_page)
        _check_ocr_density(md, len(per_page), info)
        structured = None
        if self.wants_structured(mode):
            structured = {
                "pages": [
                    {"page": i, "markdown": text} for i, text in enumerate(per_page, start=1)
                ],
                "page_count": len(per_page),
            }
        metadata = clean({**info.get("doc_props", {}), "page_count": len(per_page)})
        return ParseResult(parser=self.name, markdown=md, structured=structured, metadata=metadata)
