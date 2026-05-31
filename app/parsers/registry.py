"""Parser registry: extension → parser, plus legacy round-trip mapping.

`NATIVE` maps a (post-conversion) extension to the parser instance that
handles it. `LEGACY` maps a non-native Office extension — legacy binary
(.doc/.xls/.ppt) and OpenDocument (.odt/.ods/.odp) — to the OOXML extension
it should be converted to (via LibreOffice) before re-dispatch.

`get_parser(ext)` is the single lookup both the API and the subprocess
worker use, so routing is identical on both sides of the process boundary.
"""

from __future__ import annotations

from ..exceptions import UnsupportedFile
from .base import BaseParser
from .csv_parser import CsvParser
from .docx import DocxParser
from .email_parser import EmlParser, MboxParser, MsgParser
from .html import HtmlParser, RtfParser
from .image import ImageParser
from .json_parser import JsonParser
from .pdf import PdfParser
from .pptx import PptxParser
from .text import MarkdownParser, TextParser
from .xlsx import XlsxParser

_PARSERS: list[BaseParser] = [
    PdfParser(),
    DocxParser(),
    PptxParser(),
    XlsxParser(),
    CsvParser(),
    EmlParser(),
    MsgParser(),
    MboxParser(),
    ImageParser(),
    TextParser(),
    MarkdownParser(),
    HtmlParser(),
    RtfParser(),
    JsonParser(),
]

# ext -> parser instance
NATIVE: dict[str, BaseParser] = {}
for _p in _PARSERS:
    for _ext in _p.extensions:
        NATIVE[_ext] = _p

# non-native Office ext -> OOXML ext to convert to (via soffice), then
# re-dispatched to the native parser. Covers legacy binary Office AND
# OpenDocument (LibreOffice's own format): soffice is the reference ODF
# implementation, so the round-trip is high-fidelity and ODF inherits the
# same treatment as the OOXML equivalents (metadata, embedded-image OCR,
# equation handling). pandoc reads .odt but not .ods/.odp, so routing all
# three through soffice keeps ODF handling uniform — see DECISIONS.md §7.
LEGACY: dict[str, str] = {
    # legacy binary Office
    ".doc": ".docx",
    ".ppt": ".pptx",
    ".pps": ".pptx",
    ".pot": ".pptx",
    ".ppsx": ".pptx",
    ".xls": ".xlsx",
    # OpenDocument (LibreOffice)
    ".odt": ".docx",
    ".ods": ".xlsx",
    ".odp": ".pptx",
}

ALL_SUPPORTED: set[str] = set(NATIVE) | set(LEGACY)


def get_parser(ext: str) -> BaseParser:
    """Return the parser for a (native) extension, or raise UnsupportedFile."""
    parser = NATIVE.get(ext.lower())
    if parser is None:
        raise UnsupportedFile(f"Unsupported extension: {ext}. Supported: {sorted(ALL_SUPPORTED)}")
    return parser
