"""End-to-end tests: POST each format to /convert and check the markdown.

These exercise the real dispatch path including the subprocess worker
(pptx/xlsx/csv/email/image run in `python -m app.worker`), so a
passing run proves the package-relative imports resolve inside the child
process too.
"""

from __future__ import annotations


def _post(client, filename: str, data: bytes, mode: str | None = None) -> dict:
    params = {"mode": mode} if mode else {}
    resp = client.post(
        "/convert",
        files={"file": (filename, data, "application/octet-stream")},
        params=params,
    )
    assert resp.status_code == 200, f"{filename}: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["filename"] == filename
    if mode in (None, "markdown", "both"):
        assert body["markdown"].strip(), f"{filename}: empty markdown"
    return body


def test_health(client):
    body = client.get("/health").json()
    # pandoc must be present for the docx/html/rtf paths; soffice only for
    # legacy formats. On the dev host both are installed.
    assert body["pandoc"] is True


def test_formats_lists_all(client):
    body = client.get("/formats").json()
    for ext in (".pdf", ".docx", ".xlsx", ".pptx", ".eml", ".png", ".txt", ".md", ".json"):
        assert ext in body["supported"]


def test_txt(client, txt_bytes):
    body = _post(client, "note.txt", txt_bytes)
    assert "é" in body["markdown"]
    assert body["parser"] == "text"


def test_markdown(client, md_bytes):
    body = _post(client, "doc.md", md_bytes)
    assert "# Heading" in body["markdown"]


def test_json(client, json_bytes):
    body = _post(client, "data.json", json_bytes)
    assert "```json" in body["markdown"]
    assert "parsers" in body["markdown"]


def test_html(client, html_bytes):
    body = _post(client, "page.html", html_bytes)
    assert "Title" in body["markdown"]
    assert body["parser"] == "pandoc-html"


def test_csv(client, csv_bytes):
    body = _post(client, "rows.csv", csv_bytes)
    assert "alice" in body["markdown"]
    assert "|" in body["markdown"]  # markdown table


def test_docx(client, docx_bytes):
    body = _post(client, "report.docx", docx_bytes)
    assert "Quarterly Report" in body["markdown"]
    assert body["parser"] == "hybrid"


def test_pptx(client, pptx_bytes):
    body = _post(client, "deck.pptx", pptx_bytes)
    assert "Deck Title" in body["markdown"]


def test_xlsx(client, xlsx_bytes):
    body = _post(client, "sales.xlsx", xlsx_bytes)
    assert "North" in body["markdown"]


def test_eml(client, eml_bytes):
    body = _post(client, "mail.eml", eml_bytes)
    md = body["markdown"]
    assert "Project kickoff" in md
    assert "alice@example.com" in md
    assert "Monday" in md


def test_image_tesseract(client, png_with_text):
    # Default engine resolves to tesseract (no Gemini key in test env).
    body = _post(client, "scan.png", png_with_text)
    assert body["parser"] == "tesseract"
    # OCR should recover the rendered text (allow minor noise).
    assert "PARSER" in body["markdown"].upper()


def test_pdf(client, pdf_bytes):
    body = _post(client, "summary.pdf", pdf_bytes)
    md = body["markdown"]
    assert "## Page 1" in md  # per-page citation marker
    assert "## Page 2" in md  # multi-page boundaries preserved
    assert "retrieval system can cite the exact" in md
    assert body["parser"] == "markitdown-pdf+pages"


# --- structured / both output modes ---------------------------------------


def test_structured_xlsx(client, xlsx_bytes):
    body = _post(client, "sales.xlsx", xlsx_bytes, mode="structured")
    assert body["mode"] == "structured"
    assert "markdown" not in body
    sheets = body["structured"]["sheets"]
    assert sheets[0]["name"] == "Sales"
    flat = [c for row in sheets[0]["rows"] for c in row]
    assert "North" in flat and "Region" in flat


def test_structured_csv(client, csv_bytes):
    body = _post(client, "rows.csv", csv_bytes, mode="structured")
    s = body["structured"]
    assert s["columns"] == ["name", "score"]
    assert ["alice", "90"] in s["rows"]


def test_structured_json(client, json_bytes):
    body = _post(client, "data.json", json_bytes, mode="structured")
    assert body["structured"]["data"]["project"] == "parsers"


def test_structured_eml(client, eml_bytes):
    body = _post(client, "mail.eml", eml_bytes, mode="structured")
    s = body["structured"]
    assert s["subject"] == "Project kickoff"
    assert "alice@example.com" in s["from"]
    assert "Monday" in s["body"]


def test_structured_pdf(client, pdf_bytes):
    body = _post(client, "summary.pdf", pdf_bytes, mode="structured")
    s = body["structured"]
    assert s["page_count"] == 2
    assert s["pages"][0]["page"] == 1


def test_both_mode(client, json_bytes):
    body = _post(client, "data.json", json_bytes, mode="both")
    assert body["mode"] == "both"
    assert "```json" in body["markdown"]
    assert body["structured"]["data"]["project"] == "parsers"


def test_invalid_mode(client, txt_bytes):
    resp = client.post(
        "/convert",
        files={"file": ("a.txt", txt_bytes, "application/octet-stream")},
        params={"mode": "bogus"},
    )
    assert resp.status_code == 400


def test_unsupported_extension(client):
    resp = client.post(
        "/convert",
        files={"file": ("weird.xyz", b"data", "application/octet-stream")},
    )
    assert resp.status_code == 415
