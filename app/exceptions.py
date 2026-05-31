"""Typed parser errors that map to specific HTTP responses.

Raised by the per-format parsers, caught by the subprocess worker (which
serializes them with an `error_kind`) and by the in-process dispatch path,
then translated to HTTP status codes in `app.py`:

  EncryptedPdfError   -> 415  (file received, variant unhandleable)
  MboxValidationError -> 415
  NeedsOcrError       -> 422  (understood, but route to OCR instead)
  UnsupportedFile     -> 415  (no parser for this extension)
"""

from __future__ import annotations


class ParserError(Exception):
    """Base class for all parser-raised errors."""


class UnsupportedFile(ParserError):
    """No parser is registered for the file's extension."""


class EncryptedPdfError(ParserError):
    """PDF requires a password we don't have.

    pdfminer accepts empty-password encrypted PDFs silently, so this only
    fires when a real password is required.
    """


class NeedsOcrError(ParserError):
    """Extracted text is too sparse to be useful for RAG.

    Image-only / scanned PDFs come through text extractors with near-empty
    output. Indexing that into a vector store produces garbage embeddings,
    so we fail loudly and let the caller route the file to an OCR service.

    Threshold: chars/page < `settings.ocr_density_threshold` (default 100;
    typed PDFs sit at 1k-3k chars/page, scanned at 0-50).
    """


class MboxValidationError(ParserError):
    """mbox file is empty, missing the `From ` signature, or over the
    message cap."""
