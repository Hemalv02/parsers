"""Centralized, env-overridable configuration for the parser service.

Every tunable here started life as a module-level constant in the
`parser-pipeline/markitdown/dockerized` experiment (timeouts, caps,
encoding ladders). They are consolidated into one pydantic-settings
model so they can be overridden per-environment via `PARSER_*` env vars
(or a `.env` file) without touching code. The defaults reproduce the
benchmark-tuned values from that experiment and from the askturing
backend — see DECISIONS.md for provenance of each number.

Both the API process (`app.py`) and the subprocess worker (`worker.py`)
import the same `settings` singleton, so a value set in the environment
is seen identically on both sides of the subprocess boundary.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PARSER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Upload limit ------------------------------------------------------
    # Reject uploads larger than this (HTTP 413) while streaming to disk, so a
    # giant file can't fill the disk or OOM a worker. 0 disables the cap.
    max_file_mb: int = 100

    # --- Security: decompression-bomb guard (OOXML zips) -------------------
    # .docx/.xlsx/.pptx are ZIP containers; the upload cap above bounds only
    # the COMPRESSED bytes, so a small archive can still expand to gigabytes
    # in memory. `app/security.py` inspects the central directory at dispatch
    # and refuses an archive that breaches any of these (HTTP 413). Generous
    # defaults: real OOXML XML compresses ~10-20x and a whole document rarely
    # exceeds tens of MB uncompressed, so legitimate files clear them easily.
    zipbomb_max_uncompressed_mb: int = 1024  # total uncompressed across entries
    zipbomb_max_entries: int = 10_000  # entry count
    zipbomb_max_ratio: int = 200  # per-entry compress ratio (entries > 1 MiB)

    # --- Security: optional external malware-scan hook ---------------------
    # Shell-style command run on every upload before parsing (and before the
    # soffice round-trip for legacy formats). The file path is appended as the
    # final argument; a non-zero exit rejects the file (HTTP 422). Unset by
    # default — no AV dependency is bundled. Example:
    #   PARSER_SCAN_COMMAND="clamdscan --no-summary --fdpass"
    scan_command: str | None = None
    scan_timeout_s: int = 60

    # --- MIME verification (front-door guard) ------------------------------
    # When true, sniff the uploaded bytes with libmagic and, for the few
    # UNAMBIGUOUS binary families (PDF, images), re-route by content when the
    # filename extension disagrees — catching mislabeled files (a PNG named
    # `.pdf`) and rescuing extension-less uploads. ZIP/OLE Office subtypes and
    # text stay extension-routed (libmagic can't tell .docx from .xlsx). Off
    # by default: routing is extension-based unless this is enabled.
    verify_mime: bool = False

    # --- Logging -----------------------------------------------------------
    # Level for both the API and the subprocess worker. DEBUG surfaces the
    # per-page / per-field graceful-degradation logs that are otherwise quiet.
    log_level: str = "INFO"

    # --- Output mode -------------------------------------------------------
    # Default representation when a request doesn't specify `?mode=`:
    #   "markdown"   - GFM markdown only (back-compatible default)
    #   "structured" - per-format structured data (rows/pages/fields) only
    #   "both"       - markdown + structured
    # Per-request override via the `mode` query param on /convert or
    # `--mode` on the CLI.
    default_output_mode: str = "markdown"

    # Max rows materialized per sheet in structured xlsx/csv output. Mirrors
    # the backend's MAX_ROWS_PER_SHEET; protects against giant spreadsheets
    # ballooning the JSON response.
    max_rows_per_sheet: int = 10_000

    # --- Subprocess / converter timeouts (seconds) -------------------------
    # markitdown (pptx/xlsx), csv, pdf, email, and image all run in the
    # isolated worker subprocess the API can SIGKILL on timeout. 60s matched
    # python-pptx's worst observed hang and pandas' CSV p99 in the original
    # 1344-file benchmark.
    markitdown_timeout_s: int = 60
    # pandoc handles RTF/HTML directly in-process. 45s mirrors the backend's
    # RtfParser ceiling so timeouts stay consistent across services that
    # ingest from the same upstream sources.
    pandoc_rtf_timeout_s: int = 45
    pandoc_html_timeout_s: int = 45
    # DOCX (hybrid pandoc + omml) stays in-process; pandoc itself is isolated.
    pandoc_docx_timeout_s: int = 120
    # LibreOffice legacy-format round-trip (.doc/.ppt/.xls -> ooxml).
    soffice_timeout_s: int = 120

    # --- PDF -> "needs OCR" threshold --------------------------------------
    # Per-page raw-text density below which we declare the PDF scanned/
    # image-only and refuse it (caller routes to an OCR service). Typed PDFs
    # sit at 1k-3k chars/page; scanned ones at 0-50 from stray metadata.
    # Backend's equivalent (_SCANNED_TEXT_THRESHOLD) is 50 chars on a single
    # page; we use a per-page average of 100 across the whole document.
    ocr_density_threshold: int = 100

    # If the extracted text layer is >this fraction CID/char-code garbage,
    # the PDF's fonts lack usable Unicode maps (glyphs render but extraction
    # yields `(cid:17)`/`/66`); route to OCR instead of returning the broken
    # text. From hybrid_parser's has_font_encoding_issues (threshold 0.3).
    pdf_font_garbage_threshold: float = 0.3

    # A PDF heavier than this many bytes/page with thin text is almost
    # certainly scanned-with-a-poor-text-layer, even if it clears the raw
    # char/page floor. Used as an extra scanned signal alongside density.
    pdf_scanned_bytes_per_page: int = 500_000

    # --- mbox caps ---------------------------------------------------------
    # Mirrors backend's MAX_MBOX_EMAILS. Above this the worker refuses the
    # archive rather than risking an OOM in the subprocess.
    mbox_max_messages: int = 10_000
    # Hard cap on serialized markdown for one mbox. A 1.9 GB mbox produced a
    # 1.9 GB response and a 6.85 GB container memory peak in stress testing.
    mbox_max_output_mb: int = 50

    # --- Image OCR ---------------------------------------------------------
    # Engine selection:
    #   "tesseract" - local pytesseract OCR (offline, deterministic, no key)
    #   "gemini"    - Gemini multimodal OCR + description (matches backend;
    #                 requires the `gemini` extra and an API key)
    #   "auto"      - gemini when a key is configured, else tesseract
    # Default is tesseract so the service works out-of-the-box with no keys.
    image_ocr_engine: str = "auto"
    tesseract_lang: str = "eng"
    image_ocr_timeout_s: int = 120
    # Gemini config (only consulted when the engine resolves to "gemini").
    # Backend uses gemini-2.5-flash for image OCR + visual analysis.
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    # Image preprocessing for the Gemini path (from backend/preprocessing.py).
    image_max_bytes: int = 5 * 1024 * 1024  # 5 MB Gemini/embedding ceiling
    image_min_dimension: int = 56

    def resolve_image_engine(self) -> str:
        """Resolve the effective image OCR engine, honoring "auto"."""
        if self.image_ocr_engine == "auto":
            return "gemini" if self.gemini_api_key else "tesseract"
        return self.image_ocr_engine


settings = Settings()
