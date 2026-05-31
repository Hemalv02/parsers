"""LibreOffice legacy-format round-trip.

No good pure-Python reader exists for legacy binary Office formats
(.doc/.ppt/.xls and friends), so we convert them to OOXML with
`soffice --headless --convert-to` and then run the normal native path.

Server-hardening over a naive `soffice --convert-to` call:

  * **Per-invocation user profile** (`-env:UserInstallation=...`). LibreOffice
    keeps a single shared profile with a lock; if a second `soffice` starts
    while one is running (concurrent requests), it forwards the job to the
    running instance and returns before our output exists — a race that
    surfaces as "soffice produced no output". A throwaway per-call profile
    dir makes every invocation a fully independent instance.
  * **`--norestore --nolockcheck --nodefault`** so a previous crash's
    recovery dialog can't wedge the headless process.
  * **`SAL_USE_VCLPLUGIN=svp`** selects the headless backend without a DISPLAY
    (also set in the Dockerfile; set here too so dev hosts work).
  * **`LD_PRELOAD` passthrough.** In sandboxes that block `AF_UNIX` sockets
    soffice hangs at startup (it talks to itself over a unix socket even for
    `--convert-to`). If a socket shim is set in the environment we pass it
    through unchanged. See DECISIONS.md §7.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from .config import settings

log = logging.getLogger("parser.soffice")


def _soffice_env() -> dict:
    """Environment for soffice: headless VCL plugin (+ inherited LD_PRELOAD)."""
    env = os.environ.copy()
    env["SAL_USE_VCLPLUGIN"] = "svp"
    return env


def soffice_convert(src: Path, target_ext: str, outdir: Path) -> Path:
    """Convert `src` to `target_ext` (e.g. "docx") inside `outdir`.

    Returns the produced path. Raises RuntimeError on a non-zero exit,
    timeout, or missing output.
    """
    # Isolated, throwaway profile under the request's tmpdir — unique per call.
    profile = outdir / ".lo_profile"
    cmd = [
        "soffice",
        f"-env:UserInstallation=file://{profile}",
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        "--convert-to",
        target_ext,
        "--outdir",
        str(outdir),
        str(src),
    ]
    log.debug("soffice convert %s -> %s", src.name, target_ext)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=settings.soffice_timeout_s,
            env=_soffice_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"soffice timed out after {settings.soffice_timeout_s}s converting "
            f"{src.name} -> {target_ext}"
        ) from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"soffice exit={proc.returncode}: {proc.stderr.decode('utf-8', errors='replace')[:500]}"
        )
    produced = outdir / (src.stem + "." + target_ext)
    if not produced.exists():
        raise RuntimeError(
            f"soffice produced no output at {produced} "
            f"(stderr: {proc.stderr.decode('utf-8', errors='replace')[:300]})"
        )
    return produced
