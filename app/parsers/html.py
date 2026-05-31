"""HTML and RTF → markdown via pandoc.

pandoc preserves heading structure, lists, tables, links, and code blocks —
strictly better RAG signal than naive HTML stripping. Scripts and styles are
dropped by pandoc's GFM writer. Matches the backend's HtmlParser/RtfParser
engine choice; same risk profile, so both use the same timeout.

Neither format carries structure beyond what pandoc emits, so the structured
representation is just the markdown under a `markdown` key.
"""

from __future__ import annotations

from pathlib import Path

from ..config import settings
from .base import BaseParser, ParseResult, run_pandoc


class HtmlParser(BaseParser):
    name = "pandoc-html"
    extensions = (".html", ".htm")

    def parse(self, path: Path, mode: str) -> ParseResult:
        md = run_pandoc("html", path.read_bytes(), settings.pandoc_html_timeout_s)
        structured = {"markdown": md} if self.wants_structured(mode) else None
        return ParseResult(parser=self.name, markdown=md, structured=structured)


class RtfParser(BaseParser):
    name = "pandoc-rtf"
    extensions = (".rtf",)

    def parse(self, path: Path, mode: str) -> ParseResult:
        md = run_pandoc("rtf", path.read_bytes(), settings.pandoc_rtf_timeout_s)
        structured = {"markdown": md} if self.wants_structured(mode) else None
        return ParseResult(parser=self.name, markdown=md, structured=structured)
