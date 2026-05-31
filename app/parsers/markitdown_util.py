"""Direct markitdown converter access (bypassing the high-level API).

`MarkItDown().convert()` runs `magika` content-sniffing, which drags in
~200 MB of `onnxruntime`. We already know the type from the extension, so
we instantiate the right converter directly — this is what lets the Docker
image strip magika/onnxruntime entirely.
"""

from __future__ import annotations

import io
from pathlib import Path

from markitdown import StreamInfo

from .base import normalize_markdown


def convert_with_markitdown(converter_cls, ext: str, path: Path) -> str:
    """Run a markitdown converter on `path` and return normalized markdown."""
    with path.open("rb") as f:
        buf = io.BytesIO(f.read())
    result = converter_cls().convert(
        file_stream=buf,
        stream_info=StreamInfo(extension=ext, filename=path.name),
    )
    return normalize_markdown(result.text_content or "")
