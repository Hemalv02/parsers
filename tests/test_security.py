"""Security-guard tests: decompression-bomb limits, XXE hardening of the
DOCX XML parse, and the optional malware-scan hook.

The guards run at the dispatch chokepoint (`app/main.py:dispatch`), so the
E2E cases POST through `/convert`; the XXE cases exercise the hardened parse
in `app/hybrid.py` directly (it's a pure function on bytes)."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.exceptions import DecompressionBombError


def _zip_with_big_entry(name: str, uncompressed_mb: int) -> bytes:
    """A ZIP whose single entry is `uncompressed_mb` of zeros — compresses to
    almost nothing, so it trips both the total-size and ratio limits."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, b"\0" * (uncompressed_mb * 1024 * 1024))
    return buf.getvalue()


def _post(client, filename: str, data: bytes):
    return client.post(
        "/convert",
        files={"file": (filename, data, "application/octet-stream")},
    )


# --- #20 decompression bomb -------------------------------------------------


def test_assert_zip_safe_total_size(tmp_path, monkeypatch):
    from app import config
    from app.security import assert_zip_safe

    monkeypatch.setattr(config.settings, "zipbomb_max_uncompressed_mb", 1)
    p = tmp_path / "bomb.docx"
    p.write_bytes(_zip_with_big_entry("word/document.xml", 4))
    with pytest.raises(DecompressionBombError):
        assert_zip_safe(p)


def test_assert_zip_safe_entry_count(tmp_path, monkeypatch):
    from app import config
    from app.security import assert_zip_safe

    monkeypatch.setattr(config.settings, "zipbomb_max_entries", 5)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(10):
            zf.writestr(f"f{i}.xml", b"x")
    p = tmp_path / "many.xlsx"
    p.write_bytes(buf.getvalue())
    with pytest.raises(DecompressionBombError):
        assert_zip_safe(p)


def test_assert_zip_safe_ignores_non_zip(tmp_path):
    from app.security import assert_zip_safe

    p = tmp_path / "note.txt"
    p.write_bytes(b"just text, not a zip")
    assert_zip_safe(p)  # no raise — deferred to the parser


def test_assert_zip_safe_allows_real_office(tmp_path, xlsx_bytes, docx_bytes, pptx_bytes):
    from app.security import assert_zip_safe

    # Real (tiny) OOXML fixtures must clear the default limits.
    for name, data in (("s.xlsx", xlsx_bytes), ("d.docx", docx_bytes), ("p.pptx", pptx_bytes)):
        p = tmp_path / name
        p.write_bytes(data)
        assert_zip_safe(p)


def test_convert_rejects_zip_bomb(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "zipbomb_max_uncompressed_mb", 1)
    resp = _post(client, "bomb.docx", _zip_with_big_entry("word/document.xml", 4))
    assert resp.status_code == 413, resp.text
    assert "decompression_bomb" in resp.text


# --- #21 XXE / entity expansion --------------------------------------------


def test_xxe_external_entity_not_resolved(tmp_path):
    from app.hybrid import _replace_math_with_sentinels

    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET_DO_NOT_LEAK")
    xml = (
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE root [ <!ENTITY xxe SYSTEM "file://{secret}"> ]>\n'
        "<root>&xxe;</root>"
    ).encode()
    out = _replace_math_with_sentinels(xml, [0], [0], [], [])
    # The hardened parser never expands the external entity, so the file
    # contents can't appear in the (re-serialized) output.
    assert b"TOPSECRET_DO_NOT_LEAK" not in out


def test_billion_laughs_no_expansion(tmp_path):
    from app.hybrid import _replace_math_with_sentinels

    xml = (
        b'<?xml version="1.0"?>\n'
        b"<!DOCTYPE root [\n"
        b'  <!ENTITY a "aaaaaaaaaa">\n'
        b'  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n'
        b'  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">\n'
        b'  <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">\n'
        b"]>\n"
        b"<root>&d;</root>"
    )
    out = _replace_math_with_sentinels(xml, [0], [0], [], [])
    # With entities unresolved there is no exponential blowup.
    assert len(out) < 10_000


def test_docx_still_parses_after_hardening(client, docx_bytes):
    # Regression: the hardened parser must not break normal DOCX conversion.
    resp = _post(client, "report.docx", docx_bytes)
    assert resp.status_code == 200, resp.text
    assert "Quarterly Report" in resp.json()["markdown"]


# --- #22 malware-scan hook --------------------------------------------------


def test_scan_hook_rejects_flagged_file(client, txt_bytes, tmp_path, monkeypatch):
    from app import config

    scanner = tmp_path / "scan.sh"
    scanner.write_text("#!/bin/sh\nexit 1\n")  # always "infected"
    monkeypatch.setattr(config.settings, "scan_command", f"/bin/sh {scanner}")
    resp = _post(client, "note.txt", txt_bytes)
    assert resp.status_code == 422, resp.text
    assert "malicious_file" in resp.text


def test_scan_hook_allows_clean_file(client, txt_bytes, tmp_path, monkeypatch):
    from app import config

    scanner = tmp_path / "ok.sh"
    scanner.write_text("#!/bin/sh\nexit 0\n")  # always clean
    monkeypatch.setattr(config.settings, "scan_command", f"/bin/sh {scanner}")
    resp = _post(client, "note.txt", txt_bytes)
    assert resp.status_code == 200, resp.text


def test_scan_hook_disabled_by_default(client, txt_bytes):
    # No scan_command set → no scanning, file parses normally.
    resp = _post(client, "note.txt", txt_bytes)
    assert resp.status_code == 200, resp.text


def test_scan_missing_binary_fails_closed(tmp_path, monkeypatch):
    from app import config
    from app.security import scan_file

    monkeypatch.setattr(config.settings, "scan_command", "/no/such/scanner-binary-xyz")
    p = tmp_path / "f.txt"
    p.write_bytes(b"data")
    # Misconfigured scanner is a hard error, never a silent unscanned pass.
    with pytest.raises(RuntimeError):
        scan_file(p)
