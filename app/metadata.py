"""Normalized, per-document metadata for RAG filtering and citation.

Each parser fills the fields it can read cheaply — title / author / created /
modified, plus a format-appropriate count (pages, slides, sheets, messages,
image dimensions). The dispatch layer then augments every result with the
detected `language` (from the markdown body) and the original `source`
filename.

Unlike `structured` (a per-format payload whose shape varies), `metadata`
uses ONE shared key vocabulary across all formats, so a RAG store can index
and filter on it uniformly. Every field is optional and omitted when a format
doesn't carry it — `clean()` strips empties so the response only advertises
what was actually found. Extraction is strictly best-effort: it must never
fail a parse, so every reader degrades to `{}` on error.

Untrusted XML (OOXML `docProps/core.xml`) is parsed with a hardened lxml
parser (no entity resolution / network / DTD), mirroring app/hybrid.py —
see DECISIONS.md §13.

Recognized keys (all optional):
    title, author, created, modified   - common document properties
    page_count                         - PDF
    slide_count                        - PPTX
    sheet_count, sheet_names           - XLSX (names only in structured/both)
    row_count, column_count            - CSV/TSV (structured/both)
    message_count                      - mbox
    message_id                         - eml
    width, height, image_format        - images
    language                           - ISO 639-1, detected from the body
    source                             - original upload filename
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

import lxml.etree as ET

from .config import settings

log = logging.getLogger("parser.metadata")

# Hardened parser for untrusted OOXML core properties — see app/hybrid.py and
# DECISIONS.md §13. OOXML uses no custom entities, so valid files are
# unaffected.
_XXE_SAFE_PARSER = ET.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    huge_tree=False,
)

# OOXML core-properties namespaces (docProps/core.xml).
_CORE_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
}


def clean(meta: dict) -> dict:
    """Drop empty/None values so the response only carries known fields."""
    return {k: v for k, v in meta.items() if v not in (None, "", [], {})}


def ooxml_core_props(path: Path) -> dict:
    """Read title/author/created/modified from an OOXML `docProps/core.xml`.

    Shared by .docx/.pptx/.xlsx — every OOXML package carries core.xml.
    Returns {} if the part is absent or unparseable; metadata is best-effort
    and must never fail a parse.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            data = zf.read("docProps/core.xml")
    except (KeyError, zipfile.BadZipFile, OSError):
        return {}
    try:
        root = ET.fromstring(data, _XXE_SAFE_PARSER)
    except ET.XMLSyntaxError:
        return {}

    def _text(xpath: str) -> str:
        el = root.find(xpath, _CORE_NS)
        return (el.text or "").strip() if el is not None and el.text else ""

    return clean(
        {
            "title": _text("dc:title"),
            "author": _text("dc:creator"),
            "created": _text("dcterms:created"),
            "modified": _text("dcterms:modified"),
        }
    )


def _zip_member_count(path: Path, pattern: re.Pattern) -> int | None:
    try:
        with zipfile.ZipFile(path) as zf:
            return sum(1 for n in zf.namelist() if pattern.match(n))
    except (zipfile.BadZipFile, OSError):
        return None


_RE_SLIDE = re.compile(r"^ppt/slides/slide\d+\.xml$")
_RE_SHEET = re.compile(r"^xl/worksheets/sheet\d+\.xml$")


def pptx_slide_count(path: Path) -> int | None:
    """Slide count from the OOXML package (counts ppt/slides/slideN.xml) —
    cheap, so it's available even in markdown mode without opening python-pptx."""
    return _zip_member_count(path, _RE_SLIDE)


def xlsx_sheet_count(path: Path) -> int | None:
    """Worksheet count from the OOXML package (counts xl/worksheets/sheetN.xml)."""
    return _zip_member_count(path, _RE_SHEET)


# PDF dates: `D:YYYYMMDDHHmmSS±HH'mm'` (PDF spec 7.9.4). Time parts optional.
_RE_PDF_DATE = re.compile(r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?")


def parse_pdf_date(value: object) -> str:
    """Best-effort PDF date string → ISO-8601-ish (`YYYY-MM-DDTHH:MM:SS`).

    Returns "" for anything unrecognized; the caller treats "" as absent.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    m = _RE_PDF_DATE.match(value.strip())
    if not m or not m.group(1):
        return ""
    y, mo, d, h, mi, s = (g or "" for g in m.groups())
    out = y
    if mo:
        out += f"-{mo}"
        if d:
            out += f"-{d}"
    if h:
        out += f"T{h}"
        if mi:
            out += f":{mi}"
            if s:
                out += f":{s}"
    return out


def detect_language(markdown: str) -> str | None:
    """Detect the dominant language (ISO 639-1) of the markdown body.

    Best-effort: returns None on empty/too-short text, undetectable content, or
    if langdetect isn't importable. Deterministic via a fixed seed (langdetect
    is otherwise randomized per-run). Gated by `settings.detect_language`.
    """
    if not settings.detect_language:
        return None
    sample = (markdown or "").strip()[: settings.language_sample_chars]
    if len(sample) < 20:
        return None
    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0
        return detect(sample)
    except Exception:
        log.debug("language detection failed/unavailable", exc_info=True)
        return None


def augment(meta: dict | None, markdown: str, source: str | None) -> dict:
    """Add dispatch-level fields (detected `language`, original `source`) to a
    parser's per-format metadata. Called once in the API/CLI layer so language
    detection runs in the parent process, not the worker subprocess."""
    out = dict(meta or {})
    if source:
        out["source"] = source
    lang = detect_language(markdown)
    if lang:
        out["language"] = lang
    return out
