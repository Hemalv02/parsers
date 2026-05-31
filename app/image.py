"""Image → markdown conversion (OCR + optional visual description).

This is the one file type the `parser-pipeline/markitdown` experiment did
not cover. The askturing backend handles images with a multimodal LLM
(Gemini 2.5 Flash) for OCR *and* visual analysis rather than classic OCR
(see backend `app/services/image/processing_service.py`). We support both:

  - "tesseract" (default fallback): local pytesseract OCR. Offline,
    deterministic, no API key. Good for text-bearing images (scans,
    screenshots of documents). Produces plain transcribed text.
  - "gemini": multimodal OCR + a short visual description, matching the
    backend. Requires the `gemini` extra (`uv sync --extra gemini`) and
    `PARSER_GEMINI_API_KEY`. Better on photos, diagrams, charts, and
    images where layout/visual context matters for retrieval.

The engine is chosen by `settings.resolve_image_engine()` ("auto" picks
gemini when a key is present, else tesseract). Run inside the worker
subprocess so a hung Pillow decode or a slow network call to Gemini is
killable on timeout, exactly like the other heavy converters.
"""

from __future__ import annotations

import hashlib
import io
import logging
import subprocess
import time

from .config import settings

log = logging.getLogger("parser.image")

# Extensions Pillow/tesseract can open. Mirrors the set the backend
# accepts; tesseract reads all of these, Pillow normalizes for Gemini.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".gif", ".bmp", ".webp"}

# Sentinels the engines emit when an image carries no extractable content —
# not worth appending to a document's markdown.
_EMPTY_OCR = {"_(no text detected in image)_", "_(no content extracted)_"}


def convert_image(data: bytes, filename: str) -> tuple[str, str, str]:
    """Convert image bytes. Returns (markdown, body_text, parser_used).

    `body_text` is the OCR/description content without the `# Image:`
    heading — used for the structured representation.
    """
    engine = settings.resolve_image_engine()
    if engine == "gemini":
        body, used = _ocr_gemini(data)
    else:
        body, used = _ocr_tesseract(data)
    md = f"# Image: {filename}\n\n{body}"
    return md, body, used


def ocr_embedded_images(images: list[tuple[str, bytes]]) -> tuple[str, dict]:
    """OCR raster images extracted from a container document (PPTX/DOCX).

    `images` is a list of `(label, bytes)` where `label` locates the image
    (e.g. "Slide 3", "image2.png"). Returns `(section_markdown, stats)`:
    `section_markdown` is a "## Embedded image text" block (or "" when there's
    nothing to add) the caller appends to the document markdown, and `stats`
    reports `images_found / images_ocred / images_skipped`.

    No-op (returns `("", {})`) unless `settings.ocr_embedded_images` is on. To
    keep cost and latency bounded it: skips images below
    `embedded_image_min_dimension`, dedups by content hash (repeated logos
    OCR'd once), caps at `embedded_images_max_per_doc`, and stops once the
    per-document `embedded_image_ocr_budget_s` wall-clock is spent. Every
    per-image failure degrades to a skip — embedded OCR never fails the parse.
    """
    if not settings.ocr_embedded_images or not images:
        return "", {}

    from PIL import Image

    deadline = time.monotonic() + settings.embedded_image_ocr_budget_s
    seen: set[str] = set()
    parts: list[str] = []
    found = len(images)
    ocred = 0
    skipped = 0

    for label, data in images:
        if ocred >= settings.embedded_images_max_per_doc or time.monotonic() > deadline:
            skipped += 1
            continue
        digest = hashlib.sha1(data).hexdigest()
        if digest in seen:
            skipped += 1
            continue
        seen.add(digest)
        try:
            with Image.open(io.BytesIO(data)) as im:
                width, height = im.size
        except Exception:
            log.debug("embedded image %s: undecodable, skipping", label, exc_info=True)
            skipped += 1
            continue
        if min(width, height) < settings.embedded_image_min_dimension:
            skipped += 1
            continue
        try:
            _md, body, _used = convert_image(data, label)
        except Exception:
            log.debug("embedded image %s: OCR failed, skipping", label, exc_info=True)
            skipped += 1
            continue
        text = (body or "").strip()
        if not text or text in _EMPTY_OCR:
            skipped += 1
            continue
        parts.append(f"### {label}\n\n{text}")
        ocred += 1

    stats = {"images_found": found, "images_ocred": ocred, "images_skipped": skipped}
    if not parts:
        return "", stats
    return "## Embedded image text\n\n" + "\n\n".join(parts), stats


# ---------------------------------------------------------------------------
# Tesseract (local, default)
# ---------------------------------------------------------------------------


def _ocr_tesseract(data: bytes) -> tuple[str, str]:
    """Local OCR by piping the image to the `tesseract` binary on stdin.

    We call tesseract directly (`tesseract - stdout`) rather than via the
    pytesseract wrapper: pytesseract writes a temp file and shells out to
    read it, which (a) breaks on hosts where leptonica can't open files
    under the process TMPDIR, and (b) crashes decoding tesseract's stderr
    when leptonica emits binary noise. Feeding bytes on stdin sidesteps
    both — no temp file, no fragile error decoding.

    The image is first normalized to PNG via Pillow (RGB/grayscale) so
    palette/alpha modes and exotic formats (TIFF/GIF/WebP) all reach
    tesseract as something leptonica reliably decodes. Output is wrapped
    with a heading so downstream RAG keeps the filename as a citation
    anchor.
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    proc = subprocess.run(
        ["tesseract", "-", "stdout", "-l", settings.tesseract_lang],
        input=png_bytes,
        capture_output=True,
        timeout=settings.image_ocr_timeout_s,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"tesseract exit={proc.returncode}: {err}")

    text = proc.stdout.decode("utf-8", errors="replace")
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    body = text or "_(no text detected in image)_"
    return body, "tesseract"


# ---------------------------------------------------------------------------
# Gemini (multimodal, optional — matches backend strategy)
# ---------------------------------------------------------------------------

_GEMINI_PROMPT = (
    "Transcribe ALL text visible in this image verbatim (OCR), preserving "
    "reading order and any table structure as GitHub-flavored markdown "
    "tables. After the transcription, add a short '## Description' section "
    "describing non-text visual content (charts, diagrams, photos) in one "
    "or two sentences. If the image contains no text, output only the "
    "description. Do not add commentary or apologies."
)


def _normalize_for_gemini(data: bytes) -> tuple[bytes, str]:
    """Normalize an image for the Gemini API. Returns (bytes, mime_type).

    Mirrors backend/preprocessing.py: Gemini accepts PNG/JPEG/WebP/HEIC but
    not TIFF/GIF, so those are re-encoded to JPEG. Oversized images are
    JPEG-compressed (then scaled) under the 5 MB ceiling. RGB conversion
    handles alpha/palette modes.
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        fmt = (img.format or "").upper()
        # Formats Gemini ingests natively and that are already small enough
        # pass through untouched.
        if fmt in ("PNG", "JPEG", "WEBP") and len(data) <= settings.image_max_bytes:
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt]
            return data, mime

        rgb = img.convert("RGB") if img.mode not in ("RGB",) else img
        for quality in (85, 70, 50):
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=quality)
            if buf.tell() <= settings.image_max_bytes:
                return buf.getvalue(), "image/jpeg"

        # Still too large: scale down iteratively (backend's approach).
        w, h = rgb.size
        for _ in range(10):
            w, h = int(w * 0.8), int(h * 0.8)
            if min(w, h) < settings.image_min_dimension:
                break
            resized = rgb.resize((w, h))
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=70)
            if buf.tell() <= settings.image_max_bytes:
                return buf.getvalue(), "image/jpeg"
        # Give up shrinking; send the smallest JPEG we produced.
        return buf.getvalue(), "image/jpeg"


def _ocr_gemini(data: bytes) -> tuple[str, str]:
    """Multimodal OCR + description via Gemini. Requires the `gemini` extra."""
    if not settings.gemini_api_key:
        raise RuntimeError("image_ocr_engine='gemini' but PARSER_GEMINI_API_KEY is unset")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("google-genai not installed — run `uv sync --extra gemini`") from exc

    img_bytes, mime = _normalize_for_gemini(data)
    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=[
            types.Part.from_bytes(data=img_bytes, mime_type=mime),
            _GEMINI_PROMPT,
        ],
    )
    body = (response.text or "").strip() or "_(no content extracted)_"
    return body, f"gemini-{settings.gemini_model}"
