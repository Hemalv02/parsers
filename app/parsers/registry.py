"""Parser registry: extension → parser, plus legacy round-trip mapping.

`NATIVE` maps a (post-conversion) extension to the parser instance that
handles it. `LEGACY` maps a legacy binary Office extension to the OOXML
extension it should be converted to (via LibreOffice) before re-dispatch.

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

# legacy binary Office ext -> OOXML ext to convert to (via soffice)
LEGACY: dict[str, str] = {
    ".doc": ".docx",
    ".ppt": ".pptx",
    ".pps": ".pptx",
    ".pot": ".pptx",
    ".ppsx": ".pptx",
    ".xls": ".xlsx",
}

ALL_SUPPORTED: set[str] = set(NATIVE) | set(LEGACY)


def get_parser(ext: str) -> BaseParser:
    """Return the parser for a (native) extension, or raise UnsupportedFile."""
    parser = NATIVE.get(ext.lower())
    if parser is None:
        raise UnsupportedFile(f"Unsupported extension: {ext}. Supported: {sorted(ALL_SUPPORTED)}")
    return parser
