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
from ..config import settings
from ..image import ocr_embedded_images
from ..metadata import ooxml_core_props
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
        section, ocr_stats = ocr_embedded_images(self._media_images(path))
        if section:
            md = f"{md}\n\n{section}"
        stats = {**stats, **ocr_stats}
        structured = {"markdown": md} if self.wants_structured(mode) else None
        metadata = ooxml_core_props(path)
        return ParseResult(
            parser=parser, markdown=md, structured=structured, stats=stats, metadata=metadata
        )

    @staticmethod
    def _media_images(path: Path) -> list[tuple[str, bytes]]:
        """(filename, bytes) for raster images under word/media. Empty unless
        embedded-image OCR is on. Runs in-process — see the config note on the
        Gemini-engine kill-safety caveat for DOCX."""
        if not settings.ocr_embedded_images:
            return []
        import zipfile

        exts = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp")
        out: list[tuple[str, bytes]] = []
        try:
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if name.startswith("word/media/") and name.lower().endswith(exts):
                        out.append((name.rsplit("/", 1)[-1], zf.read(name)))
        except Exception:
            return []
        return out
