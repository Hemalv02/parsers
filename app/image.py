"""Image → markdown conversion (OCR + optional visual description).

This is the one file type the `parser-pipeline/markitdown` experiment did
not cover. The askturing backend handles images with a multimodal LLM
(Gemini 2.5 Flash) for OCR *and* visual analysis rather than classic OCR
(see backend `app/services/image/processing_service.py`). We support both:

  - "tesseract" (default fallback): local pytesseract OCR. Offline,
    deterministic, no API key. Good for text-bearing images (scans,
    screenshots of documents). Produces plain transcribed text.
  - "gemini": the backend's strategy, ported verbatim from
    `app/services/image/processing_service.py` — TWO structured Gemini calls,
    an OCR pass (`_OCRResult`: has_meaningful_text / document_type /
    extracted_text / confidence) and a visual-analysis pass
    (`_VisualAnalysisResult`: summary / detected_objects / scene_type) —
    combined into one searchable block (`_combine`, mirroring the backend's
    `get_searchable_text`). Token usage is tracked for cost. Requires the
    `gemini` extra (`uv sync --extra gemini`) and `PARSER_GEMINI_API_KEY`.

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

# Gemini supports PNG/JPEG/WebP/HEIC but not TIFF/GIF (backend's note).
_GEMINI_NATIVE = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}

# Extensions Pillow/tesseract can open. Mirrors the set the backend
# accepts; tesseract reads all of these, Pillow normalizes for Gemini.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".gif", ".bmp", ".webp"}

# Sentinels the engines emit when an image carries no extractable content —
# not worth appending to a document's markdown.
_EMPTY_OCR = {"_(no text detected in image)_", "_(no content extracted)_"}


def convert_image(data: bytes, filename: str) -> tuple[str, str, str, dict]:
    """Convert image bytes. Returns (markdown, body_text, parser_used, fields).

    `body_text` is the OCR/description content without the `# Image:` heading
    (used for the structured representation). `fields` carries the engine's
    extra signals — empty for tesseract; for Gemini it mirrors the backend:
    has_meaningful_text, document_type, scene_type, detected_objects,
    confidence, and token usage.
    """
    engine = settings.resolve_image_engine()
    if engine == "gemini":
        body, used, fields = _ocr_gemini(data)
    else:
        body, used, fields = _ocr_tesseract(data)
    md = f"# Image: {filename}\n\n{body}"
    return md, body, used, fields


def _to_rgb(img):
    """Convert to RGB, compositing transparency onto WHITE (backend's
    `_convert_to_rgb`). A plain `.convert("RGB")` turns alpha/palette
    transparency black, which wrecks OCR on screenshots/logos — white-
    background compositing preserves legibility."""
    from PIL import Image

    if img.mode == "RGB":
        return img
    if img.mode == "L":
        return img.convert("RGB")
    if img.mode in ("RGBA", "P"):
        rgba = img.convert("RGBA") if img.mode == "P" else img
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[3] if "A" in rgba.getbands() else None)
        return background
    return img.convert("RGB")


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
            _md, body, _used, _fields = convert_image(data, label)
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


def _ocr_tesseract(data: bytes) -> tuple[str, str, dict]:
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
        img = _to_rgb(img) if img.mode != "L" else img
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
    return body, "tesseract", {}


# ---------------------------------------------------------------------------
# Gemini (multimodal, optional) — ported from the askturing backend
#   app/services/image/processing_service.py: two structured passes (OCR +
#   visual analysis) combined into one searchable block. Prompts and schemas
#   are kept identical so images parse the same here as in the backend.
# ---------------------------------------------------------------------------

# Prompts from the backend (OCR_PROMPT / VISUAL_PROMPT), kept textually
# identical — written as adjacent string literals only to satisfy the line
# length limit (no newlines inserted mid-sentence).
_OCR_PROMPT = (
    "Analyze this image and extract all text content.\n"
    "\n"
    "If the image contains readable text (printed or handwritten), extract it "
    "completely in proper reading order, preserving the logical structure and flow.\n"
    "\n"
    "If the image does not contain meaningful text (e.g., it's a photo, artwork, "
    "diagram without text, or the text is completely illegible), indicate that no "
    "meaningful text is present.\n"
    "\n"
    "For screenshots, extract all visible text including UI elements, labels, and content.\n"
    "For documents, preserve paragraph structure and headings.\n"
    "For handwritten notes, do your best to transcribe legibly written text."
)

_VISUAL_PROMPT = (
    "Analyze this image and provide a detailed visual description for search "
    "and retrieval.\n"
    "\n"
    "1. Provide a comprehensive visual summary (3-5 sentences) describing the "
    "content, context, and purpose of the image. Include:\n"
    "   - What the image shows and depicts\n"
    "   - The setting, environment, or context\n"
    "   - Key activities, actions, or interactions if present\n"
    "   - The mood, style, or presentation (e.g., professional, casual, "
    "technical, artistic)\n"
    "\n"
    "2. List all significant objects, people, colors, text elements, UI "
    "components, and visual elements you can identify. Be thorough.\n"
    "\n"
    "3. Identify the specific type of scene or content (e.g., business document, "
    "product photo, user interface screenshot, data visualization, architectural "
    "diagram, landscape photo, technical schematic, presentation slide, etc.).\n"
    "\n"
    "Be descriptive and specific. The goal is to capture enough detail so that "
    "someone searching for this image using text queries will be able to find it."
)


def _normalize_for_gemini(data: bytes) -> tuple[bytes, str]:
    """Normalize an image for the Gemini API. Returns (bytes, mime_type).

    Mirrors the backend's `normalize_image_format` (TIFF/GIF → JPEG, since
    Gemini doesn't accept them) plus our own size ceiling: oversized PNG/JPEG/
    WebP are re-encoded — progressively lower JPEG quality, then scaled — under
    `image_max_bytes`. Transparency is composited onto white via `_to_rgb`.
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        fmt = (img.format or "").upper()
        # Native + already small enough → pass through untouched.
        if fmt in _GEMINI_NATIVE and len(data) <= settings.image_max_bytes:
            return data, _GEMINI_NATIVE[fmt]

        rgb = _to_rgb(img)
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
        return buf.getvalue(), "image/jpeg"


def _token_usage(response) -> dict:
    """Extract Gemini token counts for cost tracking (backend's
    `_extract_token_usage`). Returns zeros if unavailable."""
    try:
        usage = getattr(response, "usage_metadata", None)
        if usage:
            inp = getattr(usage, "prompt_token_count", 0) or 0
            out = getattr(usage, "candidates_token_count", 0) or 0
            return {
                "input_tokens": inp,
                "output_tokens": out,
                "total_tokens": getattr(usage, "total_token_count", 0) or (inp + out),
            }
    except Exception:
        log.debug("token usage extraction failed", exc_info=True)
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _combine(ocr, visual) -> str:
    """Combine OCR + visual analysis into one searchable markdown block,
    mirroring the backend's `ImageProcessingResult.get_searchable_text`:
    visual summary, then detected elements, scene type, then the OCR text."""
    parts: list[str] = []
    if visual is not None:
        if visual.summary:
            parts.append(visual.summary)
        if visual.detected_objects:
            parts.append("**Visual elements:** " + ", ".join(visual.detected_objects))
        if visual.scene_type:
            parts.append(f"**Type:** {visual.scene_type}")
    if ocr.has_meaningful_text and ocr.extracted_text:
        parts.append(f"## Extracted text\n\n{ocr.extracted_text}")
    return "\n\n".join(parts).strip()


def _ocr_gemini(data: bytes) -> tuple[str, str, dict]:
    """OCR + visual analysis via Gemini — the backend's pipeline.

    Two structured calls (OCR, then visual analysis unless disabled), combined
    into one searchable block. Returns (body, engine_label, fields) where
    `fields` carries has_meaningful_text / document_type / scene_type /
    detected_objects / confidence and summed token usage.
    """
    if not settings.gemini_api_key:
        raise RuntimeError("image_ocr_engine='gemini' but PARSER_GEMINI_API_KEY is unset")
    try:
        from google import genai
        from google.genai import types
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("google-genai not installed — run `uv sync --extra gemini`") from exc

    # Structured-output schemas, identical to the backend's.
    class _OCRResult(BaseModel):
        has_meaningful_text: bool
        document_type: str | None = None
        extracted_text: str | None = None
        confidence: str = "low"

    class _VisualResult(BaseModel):
        summary: str = ""
        detected_objects: list[str] = Field(default_factory=list)
        scene_type: str | None = None

    img_bytes, mime = _normalize_for_gemini(data)
    client = genai.Client(api_key=settings.gemini_api_key)

    def _call(prompt: str, schema):
        resp = client.models.generate_content(
            model=settings.gemini_model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(data=img_bytes, mime_type=mime),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        text = resp.text
        if text:
            parsed = schema.model_validate_json(text)
        elif schema is _OCRResult:
            parsed = schema(has_meaningful_text=False)  # required field; no JSON came back
        else:
            parsed = schema()
        return parsed, _token_usage(resp)

    ocr, ocr_tok = _call(_OCR_PROMPT, _OCRResult)
    visual = None
    vis_tok = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if settings.image_visual_analysis:
        visual, vis_tok = _call(_VISUAL_PROMPT, _VisualResult)

    body = _combine(ocr, visual) or "_(no content extracted)_"
    fields = {
        "has_meaningful_text": ocr.has_meaningful_text,
        "document_type": ocr.document_type,
        "confidence": ocr.confidence,
        "input_tokens": ocr_tok["input_tokens"] + vis_tok["input_tokens"],
        "output_tokens": ocr_tok["output_tokens"] + vis_tok["output_tokens"],
        "total_tokens": ocr_tok["total_tokens"] + vis_tok["total_tokens"],
    }
    if visual is not None:
        fields["scene_type"] = visual.scene_type
        fields["detected_objects"] = visual.detected_objects
    return body, f"gemini-{settings.gemini_model}", fields
