"""Image OCR parser. Delegates to the engine logic in `..image`
(tesseract by default, Gemini multimodal when configured). Structured form
exposes the raw extracted text and which engine produced it."""

from __future__ import annotations

from pathlib import Path

from ..image import IMAGE_EXTS, convert_image
from .base import BaseParser, ParseResult


class ImageParser(BaseParser):
    name = "image"
    extensions = tuple(sorted(IMAGE_EXTS))
    isolation = True

    def parse(self, path: Path, mode: str) -> ParseResult:
        md, body, used = convert_image(path.read_bytes(), path.name)
        structured = {"text": body, "engine": used} if self.wants_structured(mode) else None
        return ParseResult(parser=used, markdown=md, structured=structured)
