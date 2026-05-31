"""Image OCR parser. Delegates to the engine logic in `..image`
(tesseract by default, Gemini multimodal when configured). Structured form
exposes the raw extracted text and which engine produced it."""

from __future__ import annotations

from pathlib import Path

from ..image import IMAGE_EXTS, convert_image
from ..metadata import clean
from .base import BaseParser, ParseResult


class ImageParser(BaseParser):
    name = "image"
    extensions = tuple(sorted(IMAGE_EXTS))
    isolation = True

    def parse(self, path: Path, mode: str) -> ParseResult:
        data = path.read_bytes()
        md, body, used = convert_image(data, path.name)
        structured = {"text": body, "engine": used} if self.wants_structured(mode) else None
        return ParseResult(
            parser=used, markdown=md, structured=structured, metadata=self._metadata(data)
        )

    @staticmethod
    def _metadata(data: bytes) -> dict:
        """Pixel dimensions + format from the image header (best-effort)."""
        import io

        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as im:
                return clean({"width": im.width, "height": im.height, "image_format": im.format})
        except Exception:
            return {}
