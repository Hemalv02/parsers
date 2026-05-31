"""DOCX → markdown via the hybrid pandoc + vendored OMML→LaTeX converter,
with a markitdown fallback.

Runs in-process: pandoc is already an isolated subprocess and the
surrounding lxml/omml work is fast and predictable, so this doesn't need
the SIGKILL-able worker. The hybrid converter's equation-conversion stats
are surfaced under `stats`. If hybrid fails (e.g. pandoc errors on a
malformed .docx), we fall back to markitdown's DocxConverter rather than
500ing. There is no richer structured representation than the markdown, so
structured mirrors it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .. import hybrid
from .base import BaseParser, ParseResult
from .markitdown_util import convert_with_markitdown

log = logging.getLogger("parser.docx")


class DocxParser(BaseParser):
    name = "hybrid"
    extensions = (".docx",)

    def parse(self, path: Path, mode: str) -> ParseResult:
        try:
            md, stats = hybrid.convert(path)
            parser = self.name
        except Exception:
            log.warning("hybrid docx convert failed, falling back to markitdown", exc_info=True)
            from markitdown.converters import DocxConverter

            md = convert_with_markitdown(DocxConverter, ".docx", path)
            stats = {}
            parser = "markitdown-docx"
        structured = {"markdown": md} if self.wants_structured(mode) else None
        return ParseResult(parser=parser, markdown=md, structured=structured, stats=stats)
