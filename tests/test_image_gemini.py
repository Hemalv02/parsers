"""Unit tests for the backend-parity Gemini image helpers.

The full two-call Gemini pipeline needs an API key + network (and the optional
`gemini` extra), so it isn't exercised here. These cover the pure pieces that
shape the output: the OCR + visual-analysis combine (mirroring the backend's
`get_searchable_text`) and the white-background RGB compositing."""

from __future__ import annotations

from types import SimpleNamespace


def _ocr(meaningful: bool, text: str | None):
    return SimpleNamespace(has_meaningful_text=meaningful, extracted_text=text)


def _visual(summary, objects, scene):
    return SimpleNamespace(summary=summary, detected_objects=objects, scene_type=scene)


def test_combine_full():
    from app.image import _combine

    out = _combine(
        _ocr(True, "Invoice #42\nTotal: $99"),
        _visual("A scanned invoice on white paper.", ["table", "logo"], "business document"),
    )
    # Order mirrors the backend: summary, elements, type, then OCR text.
    assert out.index("A scanned invoice") < out.index("**Visual elements:**")
    assert "**Visual elements:** table, logo" in out
    assert "**Type:** business document" in out
    assert "## Extracted text" in out
    assert "Invoice #42" in out


def test_combine_ocr_only():
    from app.image import _combine

    out = _combine(_ocr(True, "just text"), None)
    assert "## Extracted text" in out
    assert "just text" in out


def test_combine_no_meaningful_text_keeps_visual():
    from app.image import _combine

    out = _combine(_ocr(False, None), _visual("A landscape photo.", ["mountain"], "photo"))
    assert "A landscape photo." in out
    assert "## Extracted text" not in out  # nothing meaningful to extract


def test_to_rgb_composites_transparency_onto_white():
    from PIL import Image

    from app.image import _to_rgb

    transparent = Image.new("RGBA", (4, 4), (0, 0, 0, 0))  # fully transparent
    rgb = _to_rgb(transparent)
    assert rgb.mode == "RGB"
    # A plain convert("RGB") would make this black; compositing makes it white.
    assert rgb.getpixel((0, 0)) == (255, 255, 255)


def test_to_rgb_passthrough_rgb():
    from PIL import Image

    from app.image import _to_rgb

    img = Image.new("RGB", (2, 2), (10, 20, 30))
    assert _to_rgb(img).getpixel((0, 0)) == (10, 20, 30)
