"""FastAPI parser service.

POST /convert  (multipart `file=...`, optional `?mode=markdown|structured|both`)
  → JSON. Routes by extension via the parser registry:
      - legacy Office (.doc/.ppt/.xls/...) is round-tripped through soffice,
        then dispatched to the native parser
      - parsers marked `isolation` run in `python -m app.worker`
        (SIGKILL-able on timeout); the rest run in-process

The `mode` selects the representation returned: markdown only, per-format
structured data only, or both. Default comes from `settings.default_output_mode`.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .config import settings
from .exceptions import UnsupportedFile
from .parsers import ALL_SUPPORTED, LEGACY, get_parser

# The worker runs as a module so it resolves package-relative imports
# regardless of install location.
WORKER_INVOCATION = [sys.executable, "-m", "app.worker"]
VALID_MODES = ("markdown", "structured", "both")

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("parser")

app = FastAPI(title="parser-service", version="0.1.0")


def _raise_for_error_kind(data: dict) -> None:
    """Translate a worker error payload into an HTTPException."""
    kind = data.get("error_kind")
    msg = str(data.get("error", "?"))[:300]
    if kind == "encrypted_pdf":
        raise HTTPException(415, f"encrypted_pdf: {msg}")
    if kind == "needs_ocr":
        raise HTTPException(422, f"needs_ocr: {msg}")
    if kind == "invalid_mbox":
        raise HTTPException(415, f"invalid_mbox: {msg}")
    if kind == "unsupported":
        raise HTTPException(415, msg)
    raise HTTPException(500, f"converter error: {msg}")


def run_isolated(path: Path, mode: str) -> dict:
    """Run an isolated parser in the SIGKILL-able worker subprocess.

    Returns the parse dict {parser, markdown, structured, stats}; maps
    structured worker errors to clean HTTP codes; turns a hang into a 504.
    """
    try:
        proc = subprocess.run(
            [*WORKER_INVOCATION, str(path), mode],
            capture_output=True,
            timeout=settings.markitdown_timeout_s,
        )
    except subprocess.TimeoutExpired:
        log.warning("worker timed out", extra={"file": path.name})
        raise HTTPException(
            504, f"converter timed out after {settings.markitdown_timeout_s}s"
        ) from None

    # Surface worker-side logs (graceful-degradation debug lines, library
    # warnings) which would otherwise be discarded on success.
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    if stderr:
        log.debug("worker[%s] stderr:\n%s", path.name, stderr)

    if proc.returncode != 0:
        raise HTTPException(
            500,
            f"worker exit={proc.returncode}: {stderr[:300]}",
        )
    try:
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, f"worker output not JSON: {exc}") from exc

    if not data.get("ok"):
        _raise_for_error_kind(data)
    return data


def dispatch(path: Path, tmpdir: Path, mode: str) -> dict:
    """Route a file to its parser and return {parser, markdown, structured,
    stats}. Legacy Office formats are converted via soffice first."""
    ext = path.suffix.lower()

    if ext in LEGACY:
        from .soffice import soffice_convert

        target = LEGACY[ext]  # e.g. ".docx"
        path = soffice_convert(path, target.lstrip("."), tmpdir)
        ext = path.suffix.lower()

    parser = get_parser(ext)  # raises UnsupportedFile -> 415 in convert()

    if parser.isolation:
        return run_isolated(path, mode)

    result = parser.parse(path, mode)
    return {
        "parser": result.parser,
        "markdown": result.markdown,
        "structured": result.structured,
        "stats": result.stats,
    }


@app.get("/health")
def health() -> dict:
    """Health probe; also verifies the external binaries are present."""
    pandoc_ok = shutil.which("pandoc") is not None
    soffice_ok = shutil.which("soffice") is not None
    tesseract_ok = shutil.which("tesseract") is not None
    return {
        "status": "ok" if pandoc_ok else "degraded",
        "pandoc": pandoc_ok,
        "soffice": soffice_ok,
        "tesseract": tesseract_ok,
    }


@app.get("/formats")
def formats() -> dict:
    return {"supported": sorted(ALL_SUPPORTED), "modes": list(VALID_MODES)}


@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    mode: str = Query(default=None),
) -> JSONResponse:
    if not file.filename:
        raise HTTPException(400, "filename required")
    ext = Path(file.filename).suffix.lower()
    # Fast path: reject unsupported extensions before reading the body. When
    # MIME verification is on we defer this until after sniffing, so a
    # mislabeled or extension-less upload can still be rescued by content.
    if not settings.verify_mime and ext not in ALL_SUPPORTED:
        raise HTTPException(415, f"unsupported extension: {ext}")

    mode = (mode or settings.default_output_mode).lower()
    if mode not in VALID_MODES:
        raise HTTPException(400, f"invalid mode: {mode}. Use one of {VALID_MODES}")

    max_bytes = settings.max_file_mb * 1024 * 1024
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / Path(file.filename).name
        # Stream to disk in chunks to avoid loading huge files into memory,
        # rejecting (413) as soon as the running total exceeds the cap so a
        # giant upload can't fill the disk.
        written = 0
        with src.open("wb") as f:
            while chunk := await file.read(1 << 20):
                written += len(chunk)
                if max_bytes and written > max_bytes:
                    raise HTTPException(413, f"file exceeds {settings.max_file_mb} MB limit")
                f.write(chunk)

        if settings.verify_mime:
            from .detect import detect_effective_ext

            eff = detect_effective_ext(src, ext)
            if eff != ext:
                rerouted = src.with_suffix(eff)
                src.rename(rerouted)
                src, ext = rerouted, eff
            if ext not in ALL_SUPPORTED:
                raise HTTPException(415, f"unsupported content/extension: {ext or '(none)'}")

        src_bytes = src.stat().st_size
        log.info("converting %s (%s bytes, mode=%s)", file.filename, src_bytes, mode)
        try:
            # dispatch() is blocking (soffice / worker subprocess.run); run it
            # in a threadpool so a single uvicorn worker can serve concurrent
            # requests instead of stalling the event loop.
            result = await run_in_threadpool(dispatch, src, tmpdir, mode)
        except HTTPException:
            raise
        except UnsupportedFile as exc:
            raise HTTPException(415, str(exc)) from None
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "converter timed out") from None
        except Exception as exc:
            log.exception("conversion failed")
            raise HTTPException(500, f"conversion failed: {type(exc).__name__}: {exc}") from exc

    # Build the response per mode. parser/filename/bytes/stats are always
    # present; markdown and/or structured depend on the requested mode.
    response: dict = {
        "parser": result["parser"],
        "mode": mode,
        "filename": file.filename,
        "bytes": src_bytes,
        "stats": result.get("stats", {}),
    }
    if mode in ("markdown", "both"):
        response["markdown"] = result["markdown"]
    if mode in ("structured", "both"):
        response["structured"] = result.get("structured")
    return JSONResponse(response)
