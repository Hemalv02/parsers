"""DOCX → markdown via the hybrid pandoc + vendored OMML→LaTeX converter.

Runs in-process: pandoc is already an isolated subprocess and the
surrounding lxml/omml work is fast and predictable, so this doesn't need
the SIGKILL-able worker. The hybrid converter's equation-conversion stats
are surfaced under `stats`. There is no richer structured representation
than the markdown, so structured mirrors it.
"""

from __future__ import annotations

from pathlib import Path

from .. import hybrid
from .base import BaseParser, ParseResult


class DocxParser(BaseParser):
    name = "hybrid"
    extensions = (".docx",)

    def parse(self, path: Path, mode: str) -> ParseResult:
        md, stats = hybrid.convert(path)
        structured = {"markdown": md} if self.wants_structured(mode) else None
        return ParseResult(parser=self.name, markdown=md, structured=structured, stats=stats)
