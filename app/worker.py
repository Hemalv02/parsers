#!/usr/bin/env python3
"""Subprocess worker: runs ONE parse in isolation.

Why a subprocess: python-pptx has been observed to hang indefinitely on
pathological files, and pandas has a long CSV tail-latency; an in-process
hang takes down the uvicorn worker until the OS reaps it. Running each
isolated parse in a child the parent can SIGKILL on timeout turns a
permanent hang into a clean 504.

Only parsers with `isolation = True` run here (pdf, office, csv, email,
image). The worker derives the extension from the path, looks the parser up
in the shared registry — identical routing to the API — and runs it.

I/O contract:
    argv:   <path> <mode>
    stdout: one JSON line:
              {"ok": true, "parser", "markdown", "structured", "stats"}
              {"ok": false, "error", ["error_kind"], ["traceback"]}
    exit:   0 unless the helper itself crashes; the caller relies on JSON.

Invoked by the API as `python -m app.worker <path> <mode>`.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from pathlib import Path

from .config import settings
from .exceptions import (
    EncryptedPdfError,
    MboxValidationError,
    NeedsOcrError,
    UnsupportedFile,
)
from .parsers import get_parser

# Worker logs go to stderr (stdout is reserved for the single JSON result
# line). The parent captures and re-logs this stderr, so worker-side
# graceful-degradation logs survive even on a successful parse.
logging.basicConfig(
    level=settings.log_level.upper(),
    stream=sys.stderr,
    format="%(levelname)s %(name)s %(message)s",
)


def main() -> int:
    if len(sys.argv) != 3:
        sys.stdout.write(json.dumps({"ok": False, "error": "bad args"}))
        return 0
    path_str, mode = sys.argv[1], sys.argv[2]
    path = Path(path_str)
    try:
        parser = get_parser(path.suffix)
        result = parser.parse(path, mode)
        sys.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "parser": result.parser,
                    "markdown": result.markdown,
                    "structured": result.structured,
                    "stats": result.stats,
                }
            )
        )
    except EncryptedPdfError as e:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error_kind": "encrypted_pdf",
                    "error": str(e),
                }
            )
        )
    except NeedsOcrError as e:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error_kind": "needs_ocr",
                    "error": str(e),
                }
            )
        )
    except MboxValidationError as e:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error_kind": "invalid_mbox",
                    "error": str(e),
                }
            )
        )
    except UnsupportedFile as e:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error_kind": "unsupported",
                    "error": str(e),
                }
            )
        )
    except Exception as e:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc()[:1000],
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
