"""Tests for the robustness fallbacks: upload size cap, CSV structured
fallback, and the pandoc→markitdown/striprtf converter fallbacks."""

from __future__ import annotations

from app.parsers import docx as docx_mod
from app.parsers import html as html_mod


def _boom(*a, **k):
    raise RuntimeError("forced converter failure")


def _post(client, filename, data, mode=None):
    params = {"mode": mode} if mode else {}
    return client.post(
        "/convert",
        files={"file": (filename, data, "application/octet-stream")},
        params=params,
    )


# --- upload size cap (#4) ---------------------------------------------------


def test_upload_cap_rejects_oversized(client, monkeypatch):
    from app import config, main

    monkeypatch.setattr(config.settings, "max_file_mb", 1)
    monkeypatch.setattr(main.settings, "max_file_mb", 1)
    big = b"x" * (1_200_000)  # ~1.14 MB > 1 MB cap
    resp = _post(client, "big.txt", big)
    assert resp.status_code == 413


def test_upload_cap_allows_normal(client, txt_bytes):
    assert _post(client, "ok.txt", txt_bytes).status_code == 200


# --- CSV structured fallback (#1): pandas ParserError → stdlib csv reader ---


def test_csv_structured_falls_back_to_stdlib(tmp_path, monkeypatch):
    # Force pandas to fail like it does on genuinely malformed CSVs, and
    # confirm _structured recovers via the stdlib csv reader (direct unit
    # test: the csv parser runs in the worker subprocess, so it can't be
    # monkeypatched through the API).
    import pandas as pd

    from app.parsers.csv_parser import CsvParser

    def raise_parser(*a, **k):
        raise pd.errors.ParserError("forced")

    monkeypatch.setattr(pd, "read_csv", raise_parser)
    p = tmp_path / "ragged.csv"
    p.write_bytes(b"a,b\n1,2,3,4\n5,6,7,8\n")
    s = CsvParser()._structured(p)
    assert s["columns"] == ["a", "b"]
    assert ["1", "2", "3", "4"] in s["rows"]


def test_csv_markdown_still_works_on_malformed(client):
    data = b"a,b\n1,2,3,4\n"
    body = _post(client, "ragged.csv", data).json()
    assert body["markdown"].strip()  # markitdown fallback on the markdown path


# --- converter fallbacks (#2, #3): force pandoc failure, expect fallback ----


def test_docx_falls_back_to_markitdown(client, monkeypatch, docx_bytes):
    monkeypatch.setattr(docx_mod.hybrid, "convert", _boom)
    body = _post(client, "report.docx", docx_bytes).json()
    assert body["parser"] == "markitdown-docx"
    assert "Quarterly Report" in body["markdown"]


def test_html_falls_back_to_markitdown(client, monkeypatch, html_bytes):
    monkeypatch.setattr(html_mod, "run_pandoc", _boom)
    body = _post(client, "page.html", html_bytes).json()
    assert body["parser"] == "markitdown-html"
    assert "Title" in body["markdown"]


def test_rtf_falls_back_to_striprtf(client, monkeypatch):
    monkeypatch.setattr(html_mod, "run_pandoc", _boom)
    body = _post(client, "note.rtf", rb"{\rtf1 hello world}").json()
    assert body["parser"] == "striprtf"
    assert "hello world" in body["markdown"]
