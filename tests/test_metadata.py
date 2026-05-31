"""Tests for the normalized per-document `metadata` returned by /convert.

Every response carries a `metadata` dict (title/author/dates, a
format-appropriate count, plus the dispatch-injected `source` filename and
detected `language`) in every mode. These round-trip through the real
dispatch path, including the worker subprocess for isolated parsers."""

from __future__ import annotations


def _post(client, filename: str, data: bytes, mode: str | None = None) -> dict:
    params = {"mode": mode} if mode else {}
    resp = client.post(
        "/convert",
        files={"file": (filename, data, "application/octet-stream")},
        params=params,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- source + language are added for every format ---------------------------


def test_metadata_present_in_markdown_mode(client, txt_bytes):
    # Metadata is mode-independent — present even in the default markdown mode.
    meta = _post(client, "note.txt", txt_bytes)["metadata"]
    assert meta["source"] == "note.txt"


def test_language_detected(client):
    body = (
        b"The parser service converts documents into markdown for retrieval. "
        b"This sentence is unmistakably written in the English language so the "
        b"detector has plenty of signal to work with."
    )
    meta = _post(client, "english.txt", body)["metadata"]
    assert meta["language"] == "en"


# --- per-format document properties + counts -------------------------------


def test_pdf_metadata(client, pdf_bytes):
    meta = _post(client, "summary.pdf", pdf_bytes)["metadata"]
    assert meta["title"] == "Annual Summary"
    assert meta["author"] == "Finance Team"
    assert meta["page_count"] == 2
    assert meta["created"].startswith("20")  # ISO-ish date from the PDF


def test_docx_metadata(client, docx_bytes):
    meta = _post(client, "report.docx", docx_bytes)["metadata"]
    assert meta["title"] == "Quarterly Report"
    assert meta["author"] == "Finance Team"


def test_pptx_slide_count(client, pptx_bytes):
    meta = _post(client, "deck.pptx", pptx_bytes)["metadata"]
    assert meta["title"] == "Deck Title"
    assert meta["slide_count"] == 1


def test_xlsx_sheet_count_and_names(client, xlsx_bytes):
    # sheet_count is always present; sheet_names only in structured/both.
    md_meta = _post(client, "sales.xlsx", xlsx_bytes)["metadata"]
    assert md_meta["sheet_count"] == 1
    assert "sheet_names" not in md_meta

    struct_meta = _post(client, "sales.xlsx", xlsx_bytes, mode="both")["metadata"]
    assert struct_meta["sheet_names"] == ["Sales"]


def test_csv_counts_only_in_structured(client, csv_bytes):
    md_meta = _post(client, "rows.csv", csv_bytes)["metadata"]
    assert "row_count" not in md_meta  # markdown mode doesn't double-parse

    struct_meta = _post(client, "rows.csv", csv_bytes, mode="structured")["metadata"]
    assert struct_meta["column_count"] == 2
    assert struct_meta["row_count"] == 2


def test_eml_metadata(client, eml_bytes):
    meta = _post(client, "mail.eml", eml_bytes)["metadata"]
    assert meta["title"] == "Project kickoff"
    assert "alice@example.com" in meta["author"]
    assert meta["message_id"] == "<abc123@example.com>"


def test_image_dimensions(client, png_with_text):
    meta = _post(client, "scan.png", png_with_text)["metadata"]
    assert meta["width"] == 320
    assert meta["height"] == 80
    assert meta["image_format"] == "PNG"


# --- unit tests for the extraction helpers ---------------------------------


def test_parse_pdf_date():
    from app.metadata import parse_pdf_date

    assert parse_pdf_date("D:20240115093000+05'00'") == "2024-01-15T09:30:00"
    assert parse_pdf_date("D:202401") == "2024-01"
    assert parse_pdf_date("garbage") == ""
    assert parse_pdf_date(None) == ""


def test_clean_drops_empties():
    from app.metadata import clean

    assert clean({"a": "x", "b": "", "c": None, "d": [], "e": 0}) == {"a": "x", "e": 0}


def test_detect_language_short_text_returns_none():
    from app.metadata import detect_language

    assert detect_language("hi") is None
    assert detect_language("") is None
