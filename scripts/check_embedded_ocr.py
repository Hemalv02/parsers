#!/usr/bin/env python3
"""Smoke-test embedded-image OCR against the live corpus.

Finds real .pptx/.docx files that actually contain raster images (by peeking
at the zip directory), runs them through the parser with embedded-image OCR
enabled, and reports whether a "## Embedded image text" section was produced
plus an OCR snippet. Usage:

    PARSER_OCR_EMBEDDED_IMAGES=true PARSER_IMAGE_OCR_ENGINE=tesseract \
        uv run python scripts/check_embedded_ocr.py <corpus_dir> [n_per_fmt]
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from app.config import settings
from app.parsers.docx import DocxParser
from app.parsers.pptx import PptxParser

_MEDIA_PREFIX = {".pptx": "ppt/media/", ".docx": "word/media/"}
_RASTER = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".bmp", ".webp")


def has_raster(path: Path, ext: str) -> int:
    """Number of raster images in the OOXML package (0 if none/unreadable)."""
    prefix = _MEDIA_PREFIX[ext]
    try:
        with zipfile.ZipFile(path) as zf:
            return sum(
                1 for n in zf.namelist() if n.startswith(prefix) and n.lower().endswith(_RASTER)
            )
    except (zipfile.BadZipFile, OSError):
        return 0


def collect(corpus: Path, ext: str, want: int, scan_cap: int) -> list[Path]:
    """First `want` files of `ext` that contain raster images. Bounded by
    `scan_cap` files examined so a huge/slow mount can't run forever."""
    out: list[Path] = []
    examined = 0
    for p in corpus.rglob(f"*{ext}"):
        if not p.is_file():
            continue
        examined += 1
        if examined > scan_cap:
            print(f"  (scan cap {scan_cap} reached; examined {examined})", flush=True)
            break
        n = has_raster(p, ext)
        print(f"  scan[{examined}] {p.name}: {n} images", flush=True)
        if n > 0:
            out.append(p)
            if len(out) >= want:
                break
    return out


def run(path: Path, ext: str) -> None:
    parser = PptxParser() if ext == ".pptx" else DocxParser()
    n_imgs = has_raster(path, ext)
    try:
        result = parser.parse(path, "markdown")
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR {type(exc).__name__}: {exc}", flush=True)
        return
    stats = result.stats or {}
    has_section = "## Embedded image text" in result.markdown
    print(f"  images_in_zip={n_imgs}  stats={stats}  section={has_section}", flush=True)
    if has_section:
        section = result.markdown.split("## Embedded image text", 1)[1].strip()
        snippet = " ".join(section.split())[:400]
        print(f"  OCR> {snippet}", flush=True)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    corpus = Path(sys.argv[1])
    want = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    print(
        f"ocr_embedded_images={settings.ocr_embedded_images}  "
        f"engine={settings.resolve_image_engine()}"
    )
    if not settings.ocr_embedded_images:
        print("!! set PARSER_OCR_EMBEDDED_IMAGES=true to exercise the feature", file=sys.stderr)

    scan_cap = int(sys.argv[3]) if len(sys.argv) > 3 else 25
    for ext in (".pptx", ".docx"):
        print(f"\n=== {ext} (files containing raster images) ===", flush=True)
        files = collect(corpus, ext, want, scan_cap)
        if not files:
            print("  (none found within scan cap)", flush=True)
        for f in files:
            print(f"- PARSE {f.name}", flush=True)
            run(f, ext)
    return 0


if __name__ == "__main__":
    sys.exit(main())
