"""Tests for the optional libmagic MIME front-door guard (verify_mime)."""

from __future__ import annotations

import io

from app.detect import detect_effective_ext


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 16), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_png_mislabeled_as_pdf_reroutes(tmp_path):
    p = tmp_path / "report.pdf"
    p.write_bytes(_png_bytes())
    assert detect_effective_ext(p, ".pdf") == ".png"


def test_correct_extension_unchanged(tmp_path, pdf_bytes):
    p = tmp_path / "doc.pdf"
    p.write_bytes(pdf_bytes)
    assert detect_effective_ext(p, ".pdf") == ".pdf"


def test_same_image_family_keeps_name(tmp_path):
    # A real PNG named .jpeg stays .jpeg-ish: same family, don't force .png.
    p = tmp_path / "pic.jpeg"
    p.write_bytes(_png_bytes())
    assert detect_effective_ext(p, ".jpeg") == ".jpeg"


def test_office_zip_not_rerouted(tmp_path, xlsx_bytes):
    # OOXML sniffs as application/zip — must stay extension-routed (.xlsx),
    # never get rerouted away from its subtype.
    p = tmp_path / "sheet.xlsx"
    p.write_bytes(xlsx_bytes)
    assert detect_effective_ext(p, ".xlsx") == ".xlsx"


def test_extensionless_png_detected(tmp_path):
    p = tmp_path / "blob"
    p.write_bytes(_png_bytes())
    assert detect_effective_ext(p, "") == ".png"


def test_convert_reroutes_when_verify_on(client, monkeypatch):
    # With verify_mime on, a PNG uploaded as .pdf is parsed as an image.
    from app import config, main

    monkeypatch.setattr(config.settings, "verify_mime", True)
    monkeypatch.setattr(main.settings, "verify_mime", True)
    resp = client.post(
        "/convert",
        files={"file": ("report.pdf", _png_bytes(), "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["parser"] == "tesseract"


def test_convert_extension_routing_default(client):
    # Default (verify_mime off): a PNG named .pdf routes to the PDF parser
    # and fails there (not silently mis-parsed). 422 needs_ocr or 500.
    resp = client.post(
        "/convert",
        files={"file": ("report.pdf", _png_bytes(), "application/octet-stream")},
    )
    assert resp.status_code in (422, 500)
