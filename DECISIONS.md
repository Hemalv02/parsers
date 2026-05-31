# Parser service — design decisions & alternatives

This service converts many document formats into **GitHub-flavored markdown**
for RAG ingestion. The goal is one normalized text representation per file,
with structure (headings, tables, page/slide boundaries) preserved well
enough that a retrieval system can chunk and cite it.

It is a port of the benchmark-tuned experiment at
`../parser-pipeline/markitdown/dockerized`, repackaged as a proper
`uv` + `src`-layout project, with email/PDF/office decisions cross-checked
against the askturing backend (`~/askturing/backend`) and an **image OCR
path added** (the experiment had none).

Every numeric constant lives in `config.py` (env-overridable via `PARSER_*`).

---

## 0. Module structure: one parser per format, registry-dispatched

**Decision.** Each format lives in its own module under `parsers/` as a
`BaseParser` subclass implementing `parse(path, mode) -> ParseResult`. A
`registry.py` maps extension → parser instance (and legacy ext → OOXML
target). Both the API (`app.py`) and the subprocess worker (`worker.py`)
route through the same `get_parser(ext)` — so dispatch is identical on both
sides of the process boundary, and adding a format is one new module +
one registry line.

**Why.** The original port grouped converters by *execution model*
(in-process in `app.py`, subprocess in `worker.py`). That's proven but
mixes formats; a per-format module layout is easier to extend, test, and
reason about. `isolation = True` on the parser class (not the file's
location) now marks which formats run in the SIGKILL-able worker, so the
safety property of §3 is preserved while the code is organized by format.

**Alternatives.** Keep the execution-model grouping — rejected in favor of
per-format modules at the user's request; the subprocess protection is
retained via the `isolation` flag.

## 1. Output: markdown AND per-format structured, switchable

**Decision.** Every parser always produces `markdown`; it optionally
produces a per-format `structured` dict (pdf→pages, xlsx/csv→rows,
pptx→slides, email→fields, image→text+engine, json→parsed data). A request
`?mode=markdown|structured|both` (default `markdown`, env
`PARSER_DEFAULT_OUTPUT_MODE`) selects what's returned. `structured` is
computed only when the mode asks for it, so the markdown path stays cheap.

**Why.** RAG embedders consume text, so markdown is the default and the
universal representation. But some consumers want the typed shape (sheet
rows for tabular reasoning, page arrays for citation, email fields for
threading). Supporting both — switchable per request — serves both without
forcing one consumer to re-parse the other's output.

**Alternatives considered.**
- *Markdown only* (the prior design). Simpler, but blocks consumers that
  need typed rows/fields. Rejected per the user's request for both modes.
- *Structured-only IR for everything.* Every downstream consumer would need
  a schema per format even just to embed text. Rejected as the default;
  offered as an opt-in mode instead.
- *One uniform structured schema across formats.* A spreadsheet, a PDF, and
  an email don't share a natural shape; forcing one loses fidelity. Each
  parser defines its own `structured` dict instead.

---

## 1b. Routing: extension-first, with an optional libmagic guard

**Decision.** Route by **filename extension** (`get_parser(ext)`), identical
in the API and the worker. Content sniffing is **off by default**.

**Why.** The upload carries the extension, so it's right in practice; and
avoiding sniffing is what lets us strip markitdown's `magika`/`onnxruntime`
(~200 MB). A mislabeled file fails loudly at its parser (the backstop), it is
never silently mis-parsed.

**Opt-in `verify_mime` (`detect.py`).** When enabled, libmagic sniffs the
bytes and overrides the extension **only for the unambiguous binary
families — PDF and images** (catches a PNG named `.pdf`, rescues
extension-less uploads). OOXML (all `application/zip`), OLE (all
`x-ole-storage`), and text (all `text/*`) stay extension-routed because
libmagic can't resolve their subtypes — there the extension is *more*
reliable. Verified on the corpus: every real `.pdf/.docx/.xls/.csv` keeps its
extension; no false reroutes.

**Alternatives.** Apache Tika / `magika` for full content detection —
rejected: heavyweight (JVM / onnxruntime) for a problem a 30-line libmagic
guard covers for the only families where sniffing is trustworthy.

---

## 2. Dependency management: `uv`, deps added via `uv add`

**Decision.** `uv` with Python 3.12, `src/` layout, `uv_build` backend.
Dependencies were added through `uv add` (never hand-edited into the
`[project]` table), split three ways:
- **runtime** (`dependencies`)
- **optional** (`[project.optional-dependencies].gemini` — heavy LLM SDK)
- **dev** (`[dependency-groups].dev` — test/lint only)

**Why.** Per the request. `uv add` keeps `uv.lock` authoritative and
resolves transitive pins. Keeping `google-genai` and the test-only fixture
generators (`fpdf2`, `python-docx`) out of the runtime set keeps the
production install lean.

**Alternatives.** Poetry / pip-tools — `uv` was requested and is faster.

---

## 3. Process architecture: API + SIGKILL-able subprocess worker

**Decision.** A FastAPI app (`app.py`) routes by extension. The risky
converters (pptx, xlsx, csv, pdf, email, image) run in a child process
(`python -m app.worker`) the API can `SIGKILL` on timeout
(default 60 s). DOCX (pandoc) and the text/html/json/rtf paths run
in-process.

**Why.** `python-pptx` was observed to hang **indefinitely** on one
pathological file in the experiment's 378-file corpus; pandas had a ~17 s
p99 on a 1344-file CSV corpus. An in-process hang takes down the uvicorn
worker until the OS reaps it. A killable child turns a permanent hang into
a clean `504`. DOCX stays in-process because pandoc is already an isolated
subprocess and the surrounding lxml/omml work is fast and predictable.

**Alternatives.**
- *Everything in-process.* Simplest, but one hostile file wedges a worker.
  Rejected — this is exactly the failure the experiment hit.
- *Thread + timeout.* Python threads can't be force-killed; a C-level hang
  in python-pptx ignores the timeout. Rejected.
- *Celery/arq queue.* Real answer at scale, but out of scope for a
  synchronous parse service; the subprocess gives the safety without the
  infra. Deferred.

---

## 4. DOCX: hybrid pandoc + vendored OMML→LaTeX (not markitdown, not docling)

**Decision.** DOCX → markdown via **pandoc** (`-t gfm --wrap=none
--track-changes=all`), with equations handled by a vendored 738-line
OMML→LaTeX module (originally docling's, originally `dwml`). Equations are
swapped for alphanumeric sentinels before pandoc runs, then restored as GFM
math (`$\`…\`$` / ` ```math `) afterward.

**Why.** pandoc has the best DOCX→markdown table/list/heading fidelity, but
**drops Office math (OMML)**. docling converts the math but pulls PyTorch +
IBM models (~5 GB). Vendoring just the XML→LaTeX state machine gets correct
equations with two small deps (`lxml`, `pylatexenc`) and no model downloads.

**Alternatives.**
- *markitdown DOCX converter.* Used only as an implicit fallback concept;
  pandoc output is cleaner. Kept markitdown for pptx/xlsx where it's strong.
- *Full docling.* Rejected on image size / cold-start.

---

## 5. PDF: per-page pdfplumber with citation markers (not whole-doc)

**Decision.** Custom per-page extraction (`worker._convert_pdf_per_page`)
on `pdfplumber`, vendoring markitdown's form/table detection
(`pdf_helpers.py`, MIT). Emits `## Page N` markers. Adds: right-margin and
page-1 banner cropping, multi-column vs glossary layout classification,
font-size heading detection, running header/footer stripping, `(cid:NNN)`
artifact removal, and hyphen-break joining.

**Why.** markitdown's stock `PdfConverter` collapses to a single pdfminer
blob when most pages are prose — destroying the page boundaries RAG needs
for citation. Per-page extraction preserves them at a small text-quality
cost on dense prose.

**Three OCR-routing triggers → `422 needs_ocr`** (we don't OCR in-process;
the 422 lets a dedicated OCR service take the file). Indexing empty/garbled
embeddings pollutes retrieval, so we refuse loudly rather than return junk:
1. **Density floor** — avg < 100 text chars/page (scanned/image-only).
   (Backend's equivalent is 50 chars on a single page; we average 100.)
2. **Broken font layer** — > 30% of the extracted text is `(cid:NN)` /
   char-code garbage. The glyphs render fine but the font's Unicode map is
   broken, so extraction yields codes. Measured on the RAW text *before*
   `_post_clean` strips `(cid:NN)` — otherwise stripping would mask a
   *partially* broken layer and we'd return silently-incomplete text and
   claim success. (Ported from `hybrid_parser`'s `has_font_encoding_issues`;
   our `_RE_CODE_ONLY_LINE` only counts all-code lines + `(cid:)` refs so
   dates/fractions like `12/25` don't false-trigger.)
3. **Heavy + thin** — > 500 KB/page with < 3× the char floor and no digital
   creator signature → scanned with a poor text layer (caught before it
   sneaks past trigger 1). A Word/LaTeX/Acrobat/LibreOffice creator string
   suppresses this, since a digital doc can legitimately be sparse.

These were chosen over `hybrid_parser`'s heavyweight approach (Apache Tika +
Docling/EasyOCR/TableFormer on GPU, with a proactive `should_use_ocr`
page-sampling analyzer). That stack is right for a GPU RunPod service but
wrong for a light MIT parse service — we kept the cheap signals from its
analyzer (creator metadata, bytes/page, char density, font garbage) without
the models. We also do **not** adopt its "force OCR for ≤100-page docs" rule
(only sensible when you have GPU OCR on hand).

**Alternatives.**
- *markitdown high-level PDF.* Loses citations. Rejected.
- *OCR every PDF.* Slow and unnecessary for born-digital PDFs; we OCR-route
  only on the three triggers above. (An opt-in local OCR fallback —
  pypdfium2 render → tesseract/Gemini — was scoped but deferred.)
- *Apache Tika / Docling.* Heavy (JVM / PyTorch+GPU). Rejected for a light
  service; see above.
- *PyMuPDF.* Faster, but AGPL; pdfplumber (MIT-ish, via pdfminer) avoids the
  license question.

---

## 6. PPTX / XLSX / CSV: markitdown + pandas

**Decision.** pptx/xlsx via markitdown's direct converters (not the
`MarkItDown()` high-level API). CSV/TSV via pandas first (utf-8 → latin-1),
markitdown's CSV converter as fallback.

**Why.** markitdown's pptx/xlsx converters are solid and produce clean
markdown tables. We call them directly to skip markitdown's `magika`
content-sniffing, which drags in ~200 MB of `onnxruntime` we don't need
(we already know the type from the extension). pandas is fastest at CSV
p50; its failure modes (inconsistent column counts) don't overlap with
markitdown's (huge single fields), so the fallback is complementary.

**Alternatives.** openpyxl/python-pptx by hand — markitdown already wraps
them well; no reason to reinvent.

---

## 7. Legacy formats (.doc/.ppt/.xls/.pps/.pot/.ppsx): LibreOffice round-trip

**Decision.** Convert legacy binary Office formats to OOXML via
`soffice --headless --convert-to`, then run the normal path.

**Why.** No good pure-Python reader for legacy binary Office formats;
LibreOffice is the de-facto converter. `.ppsx` (slideshow OOXML) is
round-tripped too because python-pptx rejects its content-type sniff.

**Server hardening** (`soffice.py`), beyond a naive `--convert-to`:
- **Per-invocation user profile** (`-env:UserInstallation=file://<tmpdir>/.lo_profile`).
  LibreOffice keeps one shared profile with a lock; a second `soffice`
  starting while one runs forwards its job to the running instance and
  returns *before our output exists* — a concurrency race that surfaces as
  "soffice produced no output". A throwaway per-request profile makes every
  call an independent instance. (The skills' `soffice.py` is single-shot and
  doesn't need this; a server does.)
- **`--norestore --nolockcheck --nodefault`** so a prior crash's recovery
  dialog can't wedge the headless process.
- **`SAL_USE_VCLPLUGIN=svp`** set in the subprocess env (not just Dockerfile),
  so dev hosts work too.
- **Explicit timeout → RuntimeError** (no silent hang).

**Sandbox gotcha (`AF_UNIX`).** soffice talks to itself over a unix socket
even for `--convert-to`; in sandboxes that block `AF_UNIX` it hangs at
startup. The skills ship an `LD_PRELOAD` shim
(`~/skills/skills/xlsx/scripts/office/soffice.py`) that swaps the socket for
a `socketpair`. We don't vendor the shim (it needs a runtime `gcc` build and
is Linux-sandbox-specific), but `soffice.py` inherits the process
environment, so setting `LD_PRELOAD` to a prebuilt shim works without code
changes. Normal Docker containers allow `AF_UNIX`, so this is only needed in
restrictive seccomp/gVisor sandboxes.

**Cost.** LibreOffice cold start is 3–5 s on first invocation per process.
Acceptable for a parse service; a UNO daemon would amortize it at high
throughput (deferred).

---

## 8. Email (.eml/.msg/.mbox): stdlib + markitdown, mirroring the backend

**Decision.**
- `.eml` → stdlib `email` (RFC 822) with RFC 2047 header decoding,
  multi-charset body fallback, and HTML→markdown via `markdownify`.
- `.msg` → markitdown's `OutlookMsgConverter` (OLE2, `olefile`).
- `.mbox` → stdlib `mailbox`, one message per `## Message N` block.

Threading headers (`In-Reply-To`, `References`) and `Message-ID` are
preserved for conversation reconstruction. Charset ladder:
`declared → utf-8 → windows-1252 → iso-8859-1 → utf-8/replace`.

**Why.** This mirrors the backend's `EmailParser`/`MboxContentExtractor`
behavior (RFC 2047 subjects, threading, charset fallback, 10k-message cap)
so files parse identically across services, while emitting markdown instead
of the backend's structured `ParsedEmail`.

**Alternative considered — backend's exact libs.** The backend uses
`mail-parser` (.eml) and `msg-parser` (.msg). We chose stdlib `email` +
markitdown's msg converter instead because (a) stdlib needs no extra dep and
gives full control over the markdown rendering, and (b) markitdown's msg
converter is already in the dependency set. The *strategy* (headers,
charsets, threading, caps) matches the backend; only the library differs.

**mbox output cap.** 50 MB serialized markdown / 10k messages — a 1.9 GB
mbox produced a 1.9 GB response and a 6.85 GB memory peak in stress testing.

---

## 9. Images (.png/.jpg/.tiff/.gif/.bmp/.webp): tesseract default, Gemini optional

**Decision.** New path (the experiment had none). Pluggable engine via
`PARSER_IMAGE_OCR_ENGINE`:
- `tesseract` (default fallback): pipe the image to the `tesseract` binary
  on **stdin** (`tesseract - stdout`). Offline, deterministic, no key.
- `gemini`: multimodal OCR + short visual description via Gemini 2.5 Flash
  (matches the backend's image strategy). Requires the `gemini` extra and
  `PARSER_GEMINI_API_KEY`.
- `auto` (the shipped default): gemini if a key is set, else tesseract.

**Why tesseract via stdin (not pytesseract).** pytesseract writes a temp
file and shells out; on this dev host leptonica could not open files under
the process `TMPDIR`, and pytesseract then crashed trying to utf-8-decode
tesseract's binary error output. Feeding PNG bytes on stdin removes the temp
file and the fragile error decoding entirely — and dropped a dependency.

**Why Gemini as the high-quality option.** The backend uses Gemini 2.5 Flash
for image OCR *and* visual analysis (charts/diagrams/photos), which classic
OCR can't describe. We keep it optional so the service runs with zero keys
by default, and matches the backend when configured.

**Alternatives.**
- *EasyOCR / PaddleOCR.* Heavy model downloads, GPU-leaning. Rejected for a
  default; tesseract is light and ubiquitous.
- *Google Document AI* (backend's PDF-OCR engine). Better for dense scans
  but GCP-coupled; Gemini covers the multimodal need with a simpler SDK.

---

## 10. Plain text / markdown / json / html / rtf

**Decision.**
- `.txt`/`.md` → decode with an encoding ladder
  (`utf-8-sig → utf-8 → latin-1 → cp1252`), pass through (text *is* markdown).
- `.json` → pretty-print inside a ` ```json ` fence (preserves arbitrary
  nesting verbatim instead of lossily flattening to tables).
- `.html` → pandoc `-f html -t gfm --sandbox` (preserves structure, drops
  scripts/styles; backend's HtmlParser choice).
- `.rtf` → pandoc `-f rtf -t gfm --sandbox`.

`pandoc --sandbox` blocks filesystem access pandoc might attempt via
`<link>`/relative `<img>`.

---

## 11. Config centralization

**Decision.** All timeouts, caps, thresholds, encodings, and the image-OCR
engine live in `config.py` (`pydantic-settings`, `PARSER_` env prefix,
`.env` support). Both the API and the worker subprocess import the same
`settings` singleton.

**Why.** The experiment scattered these as module constants in three files.
Centralizing makes them env-tunable per deployment without code changes and
documents provenance in one place.

---

## 12. Logging, error handling & no-silent-failure

**Boundary error handling (nothing is swallowed at the edge).** Every parse
path resolves to a typed outcome:
- `app.py /convert` catches `HTTPException` (re-raise), `UnsupportedFile`
  (→415), `subprocess.TimeoutExpired` (→504), and any other `Exception`
  (→500, `log.exception`). No `except: pass`.
- The worker maps typed errors to `error_kind` JSON
  (`encrypted_pdf`→415, `needs_ocr`→422, `invalid_mbox`→415,
  `unsupported`→415) and everything else to `{ok:false, error, traceback}`.
  `run_isolated` turns a non-zero exit / non-JSON stdout into a 500 and a
  hang into a 504 — so a worker failure is always surfaced, never silent.

**Graceful degradations are now logged, not silent.** The PDF/email/DOCX
paths intentionally degrade on a *part* of a document (a page whose
`extract_words` fails, a header that won't RFC-2047-decode, an OMML equation
that won't convert) rather than failing the whole file. Each such fallback
now emits a `log.debug(..., exc_info=True)` with context, so at
`PARSER_LOG_LEVEL=DEBUG` every degradation is traceable. (DOCX equation
misses are *also* counted in the hybrid `stats`.)

**Worker logs survive.** The worker logs to stderr (stdout is reserved for
its single JSON line); `run_isolated` captures that stderr and re-emits it
via `log.debug` even on success — otherwise worker-side degradation logs
would vanish whenever the parse "worked".

**Known residual.** Empty-but-valid input (e.g. a DOCX with no text) returns
200 with empty markdown — treated as "no content", not a failure. Only PDFs
have an emptiness guard (the `needs_ocr` density check), because empty PDF
text specifically signals a scanned source worth re-routing.

---

## 13. Security hardening for untrusted uploads (zip-bomb, XXE, AV hook)

**Context.** The service parses arbitrary uploaded files, so every parser is
an untrusted-input surface. Three concrete exposures were closed.

**Decision — single dispatch chokepoint.** The guards live in `app/security.py`
and run at the top of `dispatch()` (`main.py`), in the parent process, *before*
any parser opens the file — whether it runs in-process (docx) or in the
SIGKILL-able worker (xlsx/pptx), and before the soffice round-trip for legacy
formats. The worker is only ever spawned from `dispatch`, so one check covers
both execution paths without duplication.

1. **Decompression bombs (`assert_zip_safe`).** `.docx/.xlsx/.pptx` are ZIP
   containers; `max_file_mb` caps only the *compressed* upload, so a small
   archive can expand to gigabytes. The DOCX hybrid path (`hybrid.preprocess`)
   reads every entry into memory **in-process**, so a bomb there OOMs the
   uvicorn worker, not the contained child. The guard inspects only the
   central directory (no extraction) and refuses (HTTP 413) on any of: total
   uncompressed bytes (`zipbomb_max_uncompressed_mb`, default 1 GB), entry
   count (`zipbomb_max_entries`, 10k), or per-entry compression ratio
   (`zipbomb_max_ratio`, 200×) for entries over 1 MiB. The 1 MiB ratio floor
   stops small, highly-compressible boilerplate (`[Content_Types].xml`) from
   false-triggering.

2. **XXE / entity expansion (`hybrid.py`).** The one untrusted-bytes XML parse
   we own is `ET.fromstring` on DOCX OOXML parts. lxml's default parser has
   `resolve_entities=True` → billion-laughs and `SYSTEM "file://…"` disclosure.
   We parse those parts with a hardened `_XXE_SAFE_PARSER`
   (`resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False`).
   OOXML uses no custom entities, so valid documents are unaffected. (The
   vendored `omml.py` `load*` helpers also parse, but they are not on our code
   path — `hybrid` calls `oMath2Latex` on elements already parsed by the
   hardened parser — so the verbatim vendor file is left untouched.)

3. **Malware scanning (`scan_file`).** Optional, off by default: when
   `PARSER_SCAN_COMMAND` is set (e.g. `clamdscan --no-summary --fdpass`), every
   upload is scanned before parsing; a non-zero exit rejects it (HTTP 422). It
   **fails closed** — a configured-but-missing scanner binary is a 500, never a
   silent unscanned pass.

**soffice macros.** Headless `--convert-to` does not execute document macros,
and the throwaway profile starts from "High" macro security, so there is no
CLI macro-hardening to add. The residual is external/linked content fetches,
mitigated by the AV scan (pre-soffice) and deployment-layer egress limits.

**Alternatives.**
- *Guard inside each OOXML parser* — rejected: would duplicate the check across
  in-process and worker paths and miss the soffice output; the dispatch
  chokepoint is one place that dominates both.
- *Bundle ClamAV / a Python AV lib* — rejected: heavy and opinionated for a
  light service. An optional external-command hook is dependency-free and lets
  each deployment choose its scanner (or none).
- *`defusedxml`* — its lxml support is deprecated/limited; configuring lxml's
  own `XMLParser` flags is the supported, dependency-free hardening.

## 14. Document metadata: one normalized dict across all formats

**Context.** A RAG store needs more than text — it filters and cites on
document properties (title, author, dates), structural counts (pages, slides,
sheets), language, and provenance. The service previously returned only
per-parse `stats` (diagnostics) and, in structured mode, format-specific
fields; none of it was a normalized, always-present metadata surface.

**Decision.** `ParseResult` gains a `metadata: dict` carried in every response
and every mode. Unlike `structured` (per-format shape), `metadata` uses ONE
shared key vocabulary (see `app/metadata.py`): `title/author/created/modified`,
a format-appropriate count (`page_count`/`slide_count`/`sheet_count`/
`message_count`), `width/height/image_format` for images, `message_id` for
email, plus `language` and `source` added by the dispatch layer. Every field
is optional; `clean()` strips empties so the response advertises only what was
found. Extraction is strictly best-effort — every reader degrades to `{}` and
never fails a parse.

**Where each field comes from.**
- PDF: the pdfplumber `metadata` info dict (Title/Author/CreationDate/ModDate;
  `parse_pdf_date` normalizes `D:YYYYMMDD…` to ISO-ish) + `page_count`.
- DOCX/PPTX/XLSX: one shared `ooxml_core_props()` reads `docProps/core.xml`
  (every OOXML package has it). Counts come cheaply from the zip directory
  (`ppt/slides/slideN.xml`, `xl/worksheets/sheetN.xml`) so they're available
  in markdown mode without opening python-pptx/openpyxl. `sheet_names` and CSV
  `row_count`/`column_count` need the full parse, so they appear only in
  `structured`/`both` (computing them in markdown mode would parse twice).
- Email: message headers (subject→title, from→author, Date→created, Message-ID).
- Images: Pillow header (dimensions + format).

**Language** is detected centrally in the API/CLI layer (`augment()`) from the
always-computed markdown, so langdetect runs in the parent process — not the
worker subprocess — and works uniformly for in-process and isolated parsers.
Deterministic (fixed seed), gated by `PARSER_DETECT_LANGUAGE`, and best-effort
(None on short/undetectable text).

**Why langdetect.** Pure-Python, no native deps, ~1 MB — fits the light/
MIT-friendly stack rule. Heavier detectors (fastText/lingua/cld3) buy accuracy
the RAG-metadata use case doesn't need.

**Alternatives.**
- *Reuse `stats`* — rejected: `stats` is diagnostics (equation counts, etc.),
  not retrievable metadata; conflating them muddies both.
- *Only expose metadata in structured mode* — rejected: a markdown-only
  embedder still wants title/language/source for filtering; cheap fields are
  always on, expensive ones (`sheet_names`/row counts) gate on the parse cost.
- *python-docx/openpyxl for OOXML props* — rejected: opening the whole document
  just for four properties is far more expensive than reading one zip member;
  `ooxml_core_props` parses only `docProps/core.xml` (with the §13 hardened
  parser, since it's untrusted XML).

**OOXML XML is parsed with the §13 XXE-safe parser** — `core.xml` is attacker-
controlled, so the same hardened lxml parser (no entity/network/DTD) is reused.

## 15. Concurrency ceiling for heavy converters (per worker)

**Context.** `convert()` runs the blocking `dispatch()` via
`run_in_threadpool` so one uvicorn worker doesn't stall its event loop (see
§3). But the threadpool (anyio's default ~40 tokens) put no ceiling on how many
heavy parses run at once: an inbound burst could fan out to dozens of
simultaneous `soffice` / `python -m app.worker` subprocesses, and with N
uvicorn workers that multiplies to N×40. The only backstop was the container
`mem_limit`, which OOM-kills the offender rather than applying back-pressure.

**Decision.** An `asyncio.Semaphore(max_concurrent_heavy_parses)` (default 4)
gates the **heavy** path only — legacy soffice round-trips and isolated-worker
formats (pdf/office/csv/email/image). Requests past the limit await the
semaphore (queue) instead of spawning another subprocess. Light in-process
formats (text/json/html/rtf/docx) acquire a `nullcontext` and run ungated, so a
quick `.txt`/`.json` is never stuck behind a soffice burst. `_is_heavy(ext)`
decides from the registry (LEGACY membership or `parser.isolation`).

**Why per-process (not global).** The semaphore lives in each worker's event
loop; effective cluster concurrency is `UVICORN_WORKERS × max_concurrent_heavy_parses`.
A cross-process ceiling would need shared state (Redis/a file lock) — overkill
for a service whose deployment already fixes the worker count. Operators size
the two knobs together against `PARSER_MEM_LIMIT` and per-file peak RSS.

**Why gate only heavy work.** The whole point of §3's threadpool move was
concurrent service from one worker; gating *everything* at 4 would needlessly
serialize trivial requests. The risk being bounded is subprocess/​memory
fan-out, which is exactly the legacy + isolated set.

**Alternatives.**
- *Bound all dispatches with one semaphore* — simpler, but throttles light
  requests for no memory benefit; rejected.
- *Lower the anyio threadpool limiter* — blunt (affects every `run_in_threadpool`
  in the process, not just parses) and not format-aware; rejected.
- *A process pool / external queue (Celery/arq)* — the real answer at scale
  (already deferred in §3); the semaphore gives back-pressure without the infra.

## Validation against a real corpus

Sampled the `bulk data` Google Drive corpus (~4,600 files) via
`scripts/benchmark.py` (10 files/extension):

| ext | result | parser |
|---|---|---|
| `.pdf` | 10/10 | `markitdown-pdf+pages` (avg 2.0 s) |
| `.docx` | 10/10 | `hybrid` (avg 0.2 s) |
| `.doc` | 10/10 | soffice→`hybrid` (avg 1.3 s) |
| `.ppt`/`.pps`/`.pot`/`.ppsx` | all | soffice→`markitdown-pptx` |
| `.pptx` | 3/4 | one hit a python-pptx hang → **clean 504** at 60 s |
| `.xls` | 10/10 | soffice→`markitdown-xlsx` |
| `.csv` | 10/10 | `pandas` (6) + `markitdown` fallback (4) |
| `.tsv` | 10/10 | `pandas` |
| `.txt` | 5/5 | `text` |
| `.xlb`, `.unknown`, extensionless | 0 | correctly `415` (non-document binaries) |

Takeaways: every supported document format parsed; the only supported-format
failure was a single pathological `.pptx` that the subprocess timeout
contained as a 504 instead of hanging the server — exactly the design intent
of §3. The corpus had no email/image files, so those paths are covered only
by the unit tests.

## Open items / deferred

- **No chunking/embedding.** This service is parse-only by request; it emits
  markdown and `## Page N`/`## Message N` boundaries a downstream chunker can
  split on.
- **No real OCR fallback for scanned PDFs** — we return `422 needs_ocr` and
  expect the caller to route to an OCR service (backend: Document AI).
- **LibreOffice cold start** not amortized (no UNO daemon).
- **markitdown pulls magika/onnxruntime** in a local `uv sync`; the Docker
  image strips them (see `Dockerfile`) since we bypass markitdown's
  auto-detection. Local dev tolerates the extra weight.
