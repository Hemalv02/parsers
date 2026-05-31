# CLAUDE.md — parser-service

Multi-format document → **markdown** parser for RAG ingestion. Read
**[DECISIONS.md](DECISIONS.md)** (design log, with alternatives) and
**[README.md](README.md)** (usage) first — this file is the working contract,
not a re-explanation of them.

Python 3.12, package lives at **`app/`** (top-level, not `src/`). Managed with
**`uv`**.

## Architecture invariants — preserve these

- **One module per format** under `app/parsers/`, each a `BaseParser` subclass
  returning `ParseResult(parser, markdown, structured, stats)`. Adding a format
  = new module in `app/parsers/` + one line in `app/parsers/registry.py`.
- **Registry-driven routing** by extension (`get_parser(ext)`), used identically
  by `app/main.py` (API) and `app/worker.py` (subprocess). Routing is
  extension-based; libmagic sniffing is opt-in (`PARSER_VERIFY_MIME`), and only
  re-routes the unambiguous PDF/image families — see `app/detect.py`.
- **`isolation = True`** on a parser → it runs in the SIGKILL-able worker
  subprocess (`python -m app.worker`); the rest run in-process. This is the
  hang/OOM containment (pptx/pandas can hang) — don't move risky parsers
  in-process.
- **markdown is the universal output**; `structured` is per-format and computed
  **only when `mode != "markdown"`**. Modes: `markdown | structured | both`.
- **Worker I/O contract**: stdout = exactly one JSON line; all logs/warnings go
  to **stderr** (the parent re-emits them). Never print to stdout in worker paths.
- Imports inside `app/` are **relative** (`.config`, `..exceptions`). Keep them
  relative so the package stays relocatable.

## Conventions we follow

- **Dependencies only via `uv add` / `uv add --dev` / `uv add --optional gemini`** —
  never hand-edit the `[project]` deps table. Keep runtime / dev / optional
  separated (heavy/LLM deps are optional or dev).
- **All tunables live in `app/config.py`** (`pydantic-settings`, `PARSER_` env
  prefix). Don't scatter magic constants in modules — add a setting, document
  its provenance, and reference it.
- **No silent failures.** Typed errors in `app/exceptions.py` map to HTTP codes
  (`EncryptedPdfError`/`MboxValidationError`/`UnsupportedFile`→415,
  `NeedsOcrError`→422). Any broad `except` that *degrades* (drops a page/field
  rather than failing) MUST `log.debug(..., exc_info=True)` with context — never
  swallow quietly.
- **Vendored code** (`app/vendor/`) is verbatim third-party; ruff/mypy exclude
  it — don't reformat or lint it.
- **Light, MIT-friendly stack is deliberate.** Do NOT add Tika (JVM), Docling /
  PyTorch, or PyMuPDF (AGPL). pdfplumber/pandoc/markitdown/stdlib cover scope and
  produce markdown. Call markitdown converters **directly** (not `MarkItDown()`,
  which pulls magika/onnxruntime ~200 MB — the Dockerfile strips those).
- **Update DECISIONS.md** when you make a design choice — record the decision
  *and the alternatives rejected*. That file is the memory; keep it current.

## Testing & checks (run before declaring done)

```bash
uv run pytest                          # fixture per format → round-trips /convert
uv run ruff check app tests scripts    # must be clean
```

- Tests generate a fixture per format and POST to `/convert`; routing/parsing
  logic that's pure (e.g. PDF OCR-routing, MIME detection) gets direct unit tests.
- **Tests shell out to `tesseract`/`soffice`** — they need real subprocess +
  filesystem access (run unsandboxed). `pandoc/tesseract/soffice/libmagic` must
  be on PATH (Docker bundles them).
- Smoke-test against real files with `scripts/benchmark.py <corpus_dir> [N]` —
  there's a ~4.6k-file corpus at the `bulk data` Google Drive mount.

## Gotchas

- **Image OCR** calls the `tesseract` binary via **stdin** (`tesseract - stdout`),
  not pytesseract — pytesseract's tempfile approach breaks under restrictive
  TMPDIRs and crashes decoding binary stderr. Keep the stdin approach.
- **soffice** (`app/soffice.py`): per-invocation `-env:UserInstallation` profile
  (concurrency-safe) + `--norestore`. In sandboxes that block `AF_UNIX`, soffice
  hangs — an `LD_PRELOAD` socket shim (see `~/skills/skills/xlsx/scripts/office/
  soffice.py`) is the fix; we pass `LD_PRELOAD` through the env.
- **Scanned/broken-font PDFs** return `422 needs_ocr` (3 triggers: density floor,
  >30% CID/char-code garbage, heavy+thin). We do **not** OCR PDFs in-process — a
  pypdfium2→tesseract/Gemini fallback was scoped but deferred.

## Reference sources (where the logic/decisions came from)

- `../parser-pipeline/markitdown/dockerized` — origin of the ported converters.
- `~/askturing/backend` — email (RFC2047/charset/threading) + image (Gemini) strategy.
- `~/askturing/hybrid_parser` — PDF OCR-routing ideas (font-encoding, metadata signals).
- `~/skills/skills/*/office/soffice.py` — soffice sandbox/AF_UNIX handling.

## Known issues / next

- `convert()` is `async def` but calls the blocking `dispatch()` directly →
  ties up the event loop. Mitigated in `docker-compose.yml` with multiple uvicorn
  workers; the proper fix is `await run_in_threadpool(dispatch, ...)`.
- Optional scanned-PDF OCR fallback (`pypdfium2` render → existing image engine)
  is designed but not implemented.
