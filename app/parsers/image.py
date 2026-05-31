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
        md, body, used, fields = convert_image(data, path.name)

        structured = None
        if self.wants_structured(mode):
            structured = {"text": body, "engine": used}
            for k in (
                "has_meaningful_text",
                "document_type",
                "scene_type",
                "detected_objects",
                "confidence",
            ):
                if fields.get(k) not in (None, "", []):
                    structured[k] = fields[k]

        # Promote the document/scene classification into the normalized metadata
        # (RAG filtering); dimensions come from the image header.
        metadata = self._metadata(data)
        for k in ("document_type", "scene_type"):
            if fields.get(k):
                metadata[k] = fields[k]

        # Surface Gemini token usage for cost tracking (backend parity).
        stats = {}
        tokens = {
            k: fields[k] for k in ("input_tokens", "output_tokens", "total_tokens") if k in fields
        }
        if any(tokens.values()):
            stats["gemini_tokens"] = tokens

        return ParseResult(
            parser=used, markdown=md, structured=structured, metadata=metadata, stats=stats
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
