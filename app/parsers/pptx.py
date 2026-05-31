"""PPTX → markdown (markitdown) + structured slides (python-pptx).

markdown keeps markitdown's per-slide rendering. The structured form is
`{"slides": [{"slide": n, "text": str, "notes": str}]}` read directly with
python-pptx (already a markitdown dependency).
"""

from __future__ import annotations

from pathlib import Path

from markitdown.converters import PptxConverter

from ..metadata import ooxml_core_props, pptx_slide_count
from .base import BaseParser, ParseResult
from .markitdown_util import convert_with_markitdown


class PptxParser(BaseParser):
    name = "markitdown-pptx"
    extensions = (".pptx",)
    isolation = True

    def parse(self, path: Path, mode: str) -> ParseResult:
        md = convert_with_markitdown(PptxConverter, ".pptx", path)
        structured = self._structured(path) if self.wants_structured(mode) else None
        metadata = ooxml_core_props(path)
        slides = pptx_slide_count(path)
        if slides is not None:
            metadata["slide_count"] = slides
        return ParseResult(parser=self.name, markdown=md, structured=structured, metadata=metadata)

    def _structured(self, path: Path) -> dict:
        from pptx import Presentation

        prs = Presentation(str(path))
        slides = []
        for i, slide in enumerate(prs.slides, start=1):
            texts = [
                shape.text_frame.text
                for shape in slide.shapes
                if shape.has_text_frame and shape.text_frame.text.strip()
            ]
            notes = ""
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text
            slides.append({"slide": i, "text": "\n".join(texts), "notes": notes})
        return {"slides": slides}
