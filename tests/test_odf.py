"""OpenDocument (.odt/.ods/.odp) support via the LibreOffice round-trip.

ODF files are converted to OOXML by soffice and then dispatched to the native
parser, so they inherit the same handling (markdown, structured, metadata) as
.docx/.xlsx/.pptx. These shell out to `soffice`; skipped if it isn't on PATH."""

from __future__ import annotations

import io
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("soffice") is None, reason="soffice (LibreOffice) not on PATH"
)


def _post(client, filename: str, data: bytes, mode: str | None = None) -> dict:
    params = {"mode": mode} if mode else {}
    resp = client.post(
        "/convert",
        files={"file": (filename, data, "application/octet-stream")},
        params=params,
    )
    assert resp.status_code == 200, f"{filename}: {resp.status_code} {resp.text}"
    return resp.json()


def _odt_bytes() -> bytes:
    from odf.opendocument import OpenDocumentText
    from odf.text import H, P

    doc = OpenDocumentText()
    doc.text.addElement(H(outlinelevel=1, text="ODT Heading"))
    doc.text.addElement(P(text="Body from an OpenDocument text file."))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _ods_bytes() -> bytes:
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableCell, TableRow
    from odf.text import P

    doc = OpenDocumentSpreadsheet()
    table = Table(name="Sheet1")
    for region, total in (("Region", "Total"), ("North", "42"), ("South", "58")):
        row = TableRow()
        for value in (region, total):
            cell = TableCell()
            cell.addElement(P(text=value))
            row.addElement(cell)
        table.addElement(row)
    doc.spreadsheet.addElement(table)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _odp_bytes() -> bytes:
    from odf.draw import Frame, Page, TextBox
    from odf.opendocument import OpenDocumentPresentation
    from odf.style import MasterPage, PageLayout, PageLayoutProperties
    from odf.text import P

    doc = OpenDocumentPresentation()
    layout = PageLayout(name="pl1")
    layout.addElement(PageLayoutProperties(pagewidth="25.4cm", pageheight="19.05cm"))
    doc.automaticstyles.addElement(layout)
    master = MasterPage(name="Default", pagelayoutname=layout)
    doc.masterstyles.addElement(master)

    page = Page(masterpagename=master)
    frame = Frame(width="20cm", height="5cm", x="2cm", y="2cm")
    box = TextBox()
    box.addElement(P(text="Impress Slide Title"))
    box.addElement(P(text="A bullet from a LibreOffice Impress presentation."))
    frame.addElement(box)
    page.addElement(frame)
    doc.presentation.addElement(page)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_formats_lists_odf(client):
    supported = client.get("/formats").json()["supported"]
    for ext in (".odt", ".ods", ".odp"):
        assert ext in supported


def test_odt_roundtrips(client):
    body = _post(client, "report.odt", _odt_bytes())
    # soffice -> .docx -> hybrid (markitdown-docx fallback also acceptable).
    assert body["parser"] in ("hybrid", "markitdown-docx")
    assert "ODT Heading" in body["markdown"]
    assert "OpenDocument text file" in body["markdown"]


def test_ods_roundtrips(client):
    body = _post(client, "sheet.ods", _ods_bytes())
    assert body["parser"] == "markitdown-xlsx"
    assert "North" in body["markdown"]
    assert "42" in body["markdown"]


def test_odp_roundtrips(client):
    # LibreOffice Impress: soffice -> .pptx -> markitdown-pptx.
    body = _post(client, "deck.odp", _odp_bytes())
    assert body["parser"] == "markitdown-pptx"
    assert "Impress Slide Title" in body["markdown"]


def test_odt_metadata_survives_roundtrip(client):
    # Core metadata written by soffice into the converted .docx is picked up
    # by the normalized metadata path.
    body = _post(client, "report.odt", _odt_bytes(), mode="both")
    assert body["metadata"]["source"] == "report.odt"
