"""JSON → pretty-printed inside a ```json fence (markdown) and the parsed
object verbatim (structured).

Why a code fence and not "convert to markdown structure": JSON has arbitrary
nesting and arrays — coercing it to lists/tables loses information. A fenced
block keeps the data verbatim while marking the language so RAG embedders can
treat it as code or data. Invalid JSON (JSONL, malformed) falls back to raw
text in a generic fence rather than failing the upload.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import BaseParser, ParseResult, decode_text


class JsonParser(BaseParser):
    name = "json"
    extensions = (".json",)

    def parse(self, path: Path, mode: str) -> ParseResult:
        text = decode_text(path.read_bytes())
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            md = f"```\n{text}\n```"
            structured = {"raw": text} if self.wants_structured(mode) else None
            return ParseResult(parser="json-raw", markdown=md, structured=structured)

        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        md = f"```json\n{pretty}\n```"
        structured = {"data": data} if self.wants_structured(mode) else None
        return ParseResult(parser="json", markdown=md, structured=structured)
