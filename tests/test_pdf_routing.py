"""Unit tests for the PDF OCR-routing signals: broken-font detection and
the metadata/bytes-per-page scanned heuristic. These are pure functions, so
we test them directly rather than synthesizing broken-font PDFs."""

from __future__ import annotations

import pytest

from app.exceptions import NeedsOcrError
from app.parsers.pdf import (
    _check_font_encoding,
    _check_ocr_density,
    _font_garbage_ratio,
)


def test_font_garbage_ratio_high_on_cid_text():
    pages = ["(cid:17)(cid:18)(cid:19)\n(cid:20)(cid:21)(cid:22)\n" * 5]
    assert _font_garbage_ratio(pages) > 0.3


def test_font_garbage_ratio_low_on_normal_text():
    pages = [
        "The quarterly report shows revenue of 1,200 on 12/25/2024 and a "
        "ratio of 3/4 across all regions. Net margin improved materially."
    ]
    # Dates/fractions like 12/25 and 3/4 must NOT register as font garbage.
    assert _font_garbage_ratio(pages) < 0.05


def test_check_font_encoding_raises():
    with pytest.raises(NeedsOcrError, match="CID/char-code garbage"):
        _check_font_encoding({"font_garbage_ratio": 0.8})


def test_check_font_encoding_ok():
    _check_font_encoding({"font_garbage_ratio": 0.0})  # no raise


def _dense_md(pages: int, chars_per_page: int) -> str:
    return "\n\n".join(f"## Page {i}\n\n{'x' * chars_per_page}" for i in range(1, pages + 1))


def test_density_floor_triggers():
    with pytest.raises(NeedsOcrError, match="chars/page"):
        _check_ocr_density(_dense_md(3, 10), 3, {"creator": "", "bytes_per_page": 1000})


def test_heavy_file_thin_text_triggers():
    # Clears the 100-char floor (150/page) but huge bytes/page + no digital
    # creator → scanned-with-poor-text-layer.
    with pytest.raises(NeedsOcrError, match="KB/page"):
        _check_ocr_density(
            _dense_md(3, 150),
            3,
            {"creator": "", "bytes_per_page": 800_000},
        )


def test_digital_creator_suppresses_heavy_trigger():
    # Same heavy/thin file, but a digital creator signature → no raise.
    _check_ocr_density(
        _dense_md(3, 150),
        3,
        {"creator": "Microsoft Word", "bytes_per_page": 800_000},
    )


def test_normal_dense_digital_ok():
    _check_ocr_density(
        _dense_md(3, 2000),
        3,
        {"creator": "LaTeX", "bytes_per_page": 50_000},
    )
