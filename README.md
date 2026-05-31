# parser-service

Multi-format document → **markdown** parser for RAG ingestion. Upload a file,
get back clean GitHub-flavored markdown with structure (headings, tables,
`## Page N` / `## Message N` boundaries) preserved for chunking and citation.

Ported and repackaged from the benchmark-tuned `parser-pipeline/markitdown`
experiment; see **[DECISIONS.md](DECISIONS.md)** for why each format is
handled the way it is, with alternatives.

## Supported formats

| Group | Extensions | Engine |
|---|---|---|
| PDF | `.pdf` | per-page `pdfplumber` + vendored form detection (`## Page N`) |
| Word | `.docx`, `.doc`* | pandoc + vendored OMML→LaTeX (equations) |
| PowerPoint | `.pptx`, `.ppt`*, `.pps`*, `.pot`*, `.ppsx`* | markitdown |
| Excel | `.xlsx`, `.xlsm`, `.xls`* | markitdown |
| Tabular | `.csv`, `.tsv` | pandas → markitdown fallback |
| Email | `.eml`, `.msg`, `.mbox`/`.mbx` | stdlib `email`/`mailbox` + markitdown (msg) |
| Image | `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.gif`, `.bmp`, `.webp` | tesseract (default) or Gemini |
| Text | `.txt`, `.md`, `.markdown` | encoding-ladder passthrough |
| Web/RTF | `.html`, `.htm`, `.rtf` | pandoc |
| Data | `.json` | fenced passthrough |

`*` legacy binary formats are round-tripped through LibreOffice (`soffice`).

## Requirements

- Python 3.12 + [`uv`](https://docs.astral.sh/uv/)
- System binaries on `PATH`:
  - **pandoc** (docx/html/rtf)
  - **tesseract** (image OCR, default engine)
  - **soffice** / LibreOffice (legacy `.doc/.ppt/.xls` only)

## Setup

```bash
uv sync                    # runtime + dev deps
uv sync --extra gemini     # + Gemini image OCR (optional)
```

## Run

```bash
# dev server
uv run parser-service serve            # http://0.0.0.0:8000

# or directly
uv run uvicorn app.main:app --reload
```

```bash
# parse one file from the CLI (prints JSON)
uv run parser-service path/to/file.pdf
```

## HTTP API

```bash
curl localhost:8000/health        # {"status","pandoc","soffice","tesseract"}
curl localhost:8000/formats       # {"supported": [...], "modes": [...]}
curl -F file=@doc.docx localhost:8000/convert | jq .
```

Response (markdown mode):
```json
{ "parser": "hybrid", "mode": "markdown", "filename": "doc.docx",
  "bytes": 67970, "stats": {...},
  "metadata": {"title": "Q3 Report", "author": "Finance", "language": "en",
               "source": "doc.docx"},
  "markdown": "..." }
```

### Document metadata

Every response carries a normalized `metadata` dict (in all modes) for RAG
filtering and citation. All fields are optional and appear only when found:

| field | from |
|---|---|
| `title`, `author`, `created`, `modified` | PDF info dict · OOXML `docProps/core.xml` · email headers |
| `page_count` / `slide_count` / `sheet_count` / `message_count` | PDF · PPTX · XLSX · mbox |
| `sheet_names`, `row_count`, `column_count` | XLSX/CSV — only in `structured`/`both` (avoids a second parse) |
| `width`, `height`, `image_format` | images |
| `message_id` | email |
| `language` | langdetect over the markdown body (ISO 639-1) |
| `source` | original upload filename |

### Output modes

`?mode=` selects the representation (default `markdown`, set by
`PARSER_DEFAULT_OUTPUT_MODE`):

| mode | response fields | per-format `structured` shape |
|---|---|---|
| `markdown` | `markdown` | — |
| `structured` | `structured` | pdf → `{pages:[{page,markdown}], page_count}` · xlsx → `{sheets:[{name,rows}]}` · csv → `{columns,rows}` · pptx → `{slides:[{slide,text,notes}]}` · email → `{subject,from,to,...,body,attachments}` · mbox → `{messages:[...]}` · image → `{text,engine}` · json → `{data}` |
| `both` | `markdown` + `structured` | as above |

```bash
curl -F file=@sales.xlsx "localhost:8000/convert?mode=structured" | jq .structured
```

Error codes: `415` unsupported/encrypted, `422` scanned PDF (`needs_ocr`),
`504` converter timeout, `400` bad mode, `500` other.

## Configuration

All timeouts, caps, and the image OCR engine are env-overridable with the
`PARSER_` prefix — see [`.env.example`](.env.example) and `config.py`.

## Tests

```bash
uv run pytest        # generates a fixture per format, round-trips through /convert
uv run ruff check src tests
```

## Docker

```bash
# compose (recommended) — builds, runs 2 uvicorn workers, healthcheck
docker compose up --build          # http://localhost:8000

# or plain docker
docker build -t parser-service .
docker run -p 8000:8000 parser-service
```

`PARSER_*` overrides come from a local `.env` (auto-loaded by compose) or the
`environment:` block in `docker-compose.yml`; knobs like `UVICORN_WORKERS`,
`PARSER_PORT`, and `PARSER_MEM_LIMIT` tune the deployment.

The image bundles LibreOffice, pandoc, tesseract, and libmagic, and strips
markitdown's `magika`/`onnxruntime` (~200 MB) since converters are dispatched
directly.

## Layout

```
app/
  main.py           FastAPI app + registry-driven dispatch + mode switch
  worker.py         generic SIGKILL-able subprocess dispatcher
  soffice.py        LibreOffice legacy-format round-trip
  config.py         pydantic-settings (PARSER_* env)
  security.py       untrusted-upload guards (zip-bomb, optional AV scan)
  metadata.py       normalized per-document metadata extraction
  exceptions.py     typed errors -> HTTP codes
  cli.py            `parser-service` entrypoint
  hybrid.py         DOCX hybrid impl (pandoc + OMML→LaTeX)
  pdf_helpers.py    vendored markitdown form/table detection (MIT)
  image.py          image OCR engines (tesseract / Gemini)
  vendor/omml/      vendored OMML→LaTeX (MIT)
  parsers/          ONE module per format, each a BaseParser subclass
    base.py         BaseParser ABC + ParseResult + shared helpers
    registry.py     extension -> parser, legacy round-trip map
    pdf.py docx.py xlsx.py pptx.py csv_parser.py
    email_parser.py image.py text.py html.py json_parser.py
scripts/benchmark.py   sample a corpus dir and report per-format results
```

Each parser implements `parse(path, mode) -> ParseResult(markdown,
structured, stats)`. `isolation = True` parsers (pdf/office/csv/email/image)
run in the worker subprocess; the rest run in-process. Adding a format =
one new module in `parsers/` registered in `registry.py`.
