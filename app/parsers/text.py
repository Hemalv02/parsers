"""Plain text and markdown. Text *is* valid markdown — decode and pass
through. Structured form is just the decoded text under a `text` key."""

from __future__ import annotations

from pathlib import Path

from .base import BaseParser, ParseResult, decode_text


class TextParser(BaseParser):
    name = "text"
    extensions = (".txt",)

    def parse(self, path: Path, mode: str) -> ParseResult:
        text = decode_text(path.read_bytes())
        structured = {"text": text} if self.wants_structured(mode) else None
        return ParseResult(parser=self.name, markdown=text, structured=structured)


class MarkdownParser(TextParser):
    name = "markdown"
    extensions = (".md", ".markdown")
