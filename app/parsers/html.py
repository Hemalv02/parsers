"""HTML and RTF → markdown via pandoc, each with a pure-Python fallback.

pandoc preserves heading structure, lists, tables, links, and code blocks —
strictly better RAG signal than naive HTML stripping. Scripts and styles are
dropped by pandoc's GFM writer. Matches the backend's HtmlParser/RtfParser
engine choice; same risk profile, so both use the same timeout.

If pandoc fails (or isn't on PATH), we fall back rather than 500:
  - HTML → markitdown's HtmlConverter (markdownify under the hood)
  - RTF  → striprtf (plain-text extraction)

Neither format carries structure beyond the text, so the structured
representation is just the markdown under a `markdown` key.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import settings
from .base import BaseParser, ParseResult, run_pandoc
from .markitdown_util import convert_with_markitdown

log = logging.getLogger("parser.html")


class HtmlParser(BaseParser):
    name = "pandoc-html"
    extensions = (".html", ".htm")

    def parse(self, path: Path, mode: str) -> ParseResult:
        try:
            md = run_pandoc("html", path.read_bytes(), settings.pandoc_html_timeout_s)
            parser = self.name
        except Exception:
            log.warning("pandoc html failed, falling back to markitdown", exc_info=True)
            from markitdown.converters import HtmlConverter

            md = convert_with_markitdown(HtmlConverter, path.suffix.lower(), path)
            parser = "markitdown-html"
        structured = {"markdown": md} if self.wants_structured(mode) else None
        return ParseResult(parser=parser, markdown=md, structured=structured)


class RtfParser(BaseParser):
    name = "pandoc-rtf"
    extensions = (".rtf",)

    def parse(self, path: Path, mode: str) -> ParseResult:
        try:
            md = run_pandoc("rtf", path.read_bytes(), settings.pandoc_rtf_timeout_s)
            parser = self.name
        except Exception:
            log.warning("pandoc rtf failed, falling back to striprtf", exc_info=True)
            from striprtf.striprtf import rtf_to_text

            md = rtf_to_text(path.read_text(encoding="utf-8", errors="replace")).strip()
            parser = "striprtf"
        structured = {"markdown": md} if self.wants_structured(mode) else None
        return ParseResult(parser=parser, markdown=md, structured=structured)
