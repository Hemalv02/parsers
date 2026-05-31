"""Security guards for untrusted uploads: decompression-bomb limits for
ZIP-container formats and an optional external malware-scan hook.

Both run at the dispatch chokepoint (`app/main.py:dispatch`), in the parent
process, BEFORE any format parser opens the file — whether that parser runs
in-process (docx) or in the SIGKILL-able worker (xlsx/pptx). Putting them
there means a hostile archive is rejected before it can expand in memory or
reach python-pptx / openpyxl / markitdown / pandoc, and a single check covers
both execution paths (the worker is only ever spawned from `dispatch`, after
these guards have run). See DECISIONS.md §13.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import zipfile
from pathlib import Path

from .config import settings
from .exceptions import DecompressionBombError, MaliciousFileError

log = logging.getLogger("parser.security")

# OOXML (.docx/.xlsx/.pptx) are ZIP containers. Only entries larger than this
# are subjected to the per-entry compression-ratio test, so small but highly
# compressible boilerplate (e.g. `[Content_Types].xml`) never false-triggers —
# a real bomb hides its payload in one or a few very large, very compressible
# entries, which clear this floor easily.
_RATIO_CHECK_FLOOR = 1 << 20  # 1 MiB


def assert_zip_safe(path: Path) -> None:
    """Reject a ZIP-container upload that would decompress into a bomb.

    Inspects only the central directory (`infolist()`) — no extraction — so
    it's cheap and runs before any parser touches the bytes. Three independent
    limits, all from `settings`:

      - total uncompressed bytes across all entries
        (`zipbomb_max_uncompressed_mb`)
      - entry count (`zipbomb_max_entries`)
      - per-entry compression ratio for entries over `_RATIO_CHECK_FLOOR`
        (`zipbomb_max_ratio`)

    A non-ZIP file (legacy OLE .doc/.xls, PDF, text, image, ...) is ignored;
    its own parser handles it. A truncated/corrupt zip is also left to the
    parser so its typed error isn't masked here.
    """
    if not zipfile.is_zipfile(path):
        return

    max_total = settings.zipbomb_max_uncompressed_mb * 1024 * 1024
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        # Not a readable zip after all — let the format parser raise its own
        # typed error rather than masking it with a security rejection.
        log.debug("zip guard: %s is not a readable zip, deferring to parser", path.name)
        return

    if len(infos) > settings.zipbomb_max_entries:
        raise DecompressionBombError(
            f"archive has {len(infos)} entries (> {settings.zipbomb_max_entries} limit)"
        )

    total = 0
    for it in infos:
        total += it.file_size
        if max_total and total > max_total:
            raise DecompressionBombError(
                f"archive decompresses to over {settings.zipbomb_max_uncompressed_mb} MB "
                f"— refusing as a decompression bomb"
            )
        if (
            it.file_size > _RATIO_CHECK_FLOOR
            and it.compress_size > 0
            and it.file_size / it.compress_size > settings.zipbomb_max_ratio
        ):
            ratio = it.file_size / it.compress_size
            raise DecompressionBombError(
                f"archive entry {it.filename!r} has a {ratio:.0f}x compression ratio "
                f"(> {settings.zipbomb_max_ratio}x limit) — decompression bomb"
            )


def scan_file(path: Path) -> None:
    """Run the configured external malware scanner on `path`, if any.

    `settings.scan_command` is a shell-style command (e.g.
    `clamdscan --no-summary --fdpass`); the file path is appended as the final
    argument. A non-zero exit means "flagged" → `MaliciousFileError` (422).
    No-op when unset, so the service ships with zero AV dependency.

    Fails CLOSED: if `scan_command` is set but its binary is missing, that's a
    misconfiguration we surface as a 500 rather than silently letting an
    unscanned file through.
    """
    cmd = (settings.scan_command or "").strip()
    if not cmd:
        return

    argv = shlex.split(cmd) + [str(path)]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=settings.scan_timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise MaliciousFileError(
            f"malware scan timed out after {settings.scan_timeout_s}s"
        ) from exc
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"PARSER_SCAN_COMMAND is set but its binary was not found: {argv[0]!r}"
        ) from exc

    if proc.returncode != 0:
        detail = proc.stdout.decode("utf-8", errors="replace").strip()[:300]
        log.warning("scanner flagged %s (exit=%d): %s", path.name, proc.returncode, detail)
        raise MaliciousFileError(
            f"file flagged by malware scanner (exit={proc.returncode}): {detail or '(no output)'}"
        )
