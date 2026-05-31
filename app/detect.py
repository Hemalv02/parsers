"""Optional content-based (libmagic) extension verification.

Routing is extension-based by default (see DECISIONS §3). When
`settings.verify_mime` is on, this sniffs the uploaded bytes and *overrides*
the filename extension **only** for the unambiguous binary families — PDF and
images — where libmagic is reliable and a mismatch is a real mislabel
(e.g. a PNG renamed `.pdf`, or an extension-less upload).

What we deliberately do NOT re-route on:
  - OOXML (`.docx/.xlsx/.pptx`) — all sniff as `application/zip`; libmagic
    can't pick the subtype, so the extension is more trustworthy.
  - OLE (`.doc/.xls/.ppt/.msg`) — all sniff as `application/x-ole-storage`.
  - text (`.txt/.md/.csv/.json/.html`) — all `text/*`; the extension carries
    the real intent (a `.json` and a `.txt` are both `text/plain`).
For those, the detected MIME is ignored and the extension wins.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("parser.detect")

# libmagic MIME → canonical extension, ONLY for families where the sniff is
# unambiguous and worth overriding a wrong extension.
_MIME_TO_EXT: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "image/x-ms-bmp": ".bmp",
    "image/webp": ".webp",
}

# Extensions that belong to an overridable family — if the filename already
# names one of these, a detected member of the SAME family shouldn't trigger a
# reroute (e.g. `.jpeg` detected as image/jpeg → keep `.jpeg`, don't force `.jpg`).
_PDF_EXTS = {".pdf"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".tiff", ".tif", ".bmp", ".webp"}


def _family(ext: str) -> str | None:
    if ext in _PDF_EXTS:
        return "pdf"
    if ext in _IMAGE_EXTS:
        return "image"
    return None


def _detected_family(detected_ext: str) -> str | None:
    return _family(detected_ext)


def detect_effective_ext(path: Path, filename_ext: str) -> str:
    """Return the extension to route on.

    Sniffs `path`; if the content is an unambiguous PDF/image whose family
    differs from `filename_ext`, returns the detected extension (a reroute).
    Otherwise returns `filename_ext` unchanged. Never raises — if libmagic is
    unavailable or errors, falls back to the filename extension.
    """
    try:
        import magic

        with path.open("rb") as f:
            head = f.read(4096)
        mime = magic.from_buffer(head, mime=True)
    except Exception:  # libmagic missing / unreadable — trust the extension
        log.debug("MIME sniff failed; keeping extension %s", filename_ext, exc_info=True)
        return filename_ext

    detected_ext = _MIME_TO_EXT.get(mime)
    if detected_ext is None:
        return filename_ext  # ambiguous (zip/ole/text/unknown) → extension wins

    detected_fam = _detected_family(detected_ext)
    if _family(filename_ext) == detected_fam:
        return filename_ext  # same family (e.g. .jpeg vs .jpg) — keep the name

    log.warning(
        "content/extension mismatch: %r sniffed as %s (%s); routing as %s",
        path.name,
        mime,
        detected_fam,
        detected_ext,
    )
    return detected_ext
