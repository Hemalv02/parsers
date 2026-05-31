"""BaseParser interface, the ParseResult value object, and shared helpers.

Every format lives in its own module under `parsers/` as a `BaseParser`
subclass. A parser always produces `markdown` (the benchmark-validated
representation); it optionally produces `structured` (a per-format dict:
rows for sheets, pages for PDFs, fields for email, ...). The requested
`mode` decides which fields the API exposes:

    "markdown"   -> {markdown}
    "structured" -> {structured}
    "both"       -> {markdown, structured}

Parsers compute `structured` only when `mode != "markdown"`, so the cheap
path stays cheap.

`isolation = True` marks a parser whose work runs in the SIGKILL-able
subprocess worker (the formats with hang/OOM risk: pdf, office, csv,
email, image). In-process parsers (text, json, html, rtf, docx) leave it
False.
"""

from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

# Modes that require the structured representation to be computed.
STRUCTURED_MODES = ("structured", "both")

# Encoding ladder for plain-text inputs. Mirrors the backend's TextParser:
# utf-8-sig first to strip any BOM (also accepts BOM-less utf-8), then
# progressively more permissive legacy encodings, finally utf-8-with-replace
# so we never reject a file outright.
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "latin-1", "cp1252")


@dataclass
class ParseResult:
    """Normalized output of a parser.

    `parser` is the engine label (e.g. "hybrid", "markitdown-pdf+pages").
    `structured` is None unless the parser built a per-format representation.
    `stats` carries optional per-parse diagnostics (e.g. hybrid equation
    counts). `metadata` is normalized, retrievable document metadata
    (title/author/dates/counts) with a key vocabulary shared across formats —
    see app/metadata.py. The dispatch layer augments it with the detected
    `language` and the original `source` filename.
    """

    parser: str
    markdown: str
    structured: dict | None = None
    stats: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


class BaseParser(ABC):
    name: str = "base"
    extensions: tuple[str, ...] = ()
    isolation: bool = False

    @abstractmethod
    def parse(self, path: Path, mode: str) -> ParseResult:
        """Parse `path`. Compute structured output only when
        `mode` is in STRUCTURED_MODES."""

    @staticmethod
    def wants_structured(mode: str) -> bool:
        return mode in STRUCTURED_MODES


def decode_text(data: bytes) -> str:
    """Decode bytes through the text encoding ladder, never raising."""
    for enc in TEXT_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_pandoc(src_format: str, data: bytes, timeout: int) -> str:
    """Convert `data` from `src_format` to GFM markdown via the pandoc CLI.

    `--sandbox` blocks filesystem access pandoc might attempt via
    `<link>`/relative `<img>`; `--wrap=none` keeps lines intact for chunking.
    """
    proc = subprocess.run(
        ["pandoc", "--sandbox", "-f", src_format, "-t", "gfm", "--wrap=none"],
        input=data,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pandoc {src_format}: {proc.stderr.decode('utf-8', errors='replace')[:300]}"
        )
    return proc.stdout.decode("utf-8", errors="replace")


def normalize_markdown(text: str) -> str:
    """MarkItDown's post-conversion normalization: rstrip lines, collapse
    runs of 3+ newlines to 2. Keeps our output byte-identical to the
    markitdown high-level API."""
    text = "\n".join(line.rstrip() for line in re.split(r"\r?\n", text))
    return re.sub(r"\n{3,}", "\n\n", text)
