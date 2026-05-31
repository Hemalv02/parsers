# Multi-format document -> markdown parser service.
# Ported from parser-pipeline/markitdown/dockerized; adds tesseract for the
# image OCR path and installs Python deps from the uv lockfile.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SAL_USE_VCLPLUGIN=svp \
    HOME=/tmp \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

# --- System packages --------------------------------------------------------
# libreoffice-{writer,calc,impress,core} : .doc/.ppt/.xls -> ooxml round-trip
# pandoc            : docx/html/rtf -> gfm markdown (CLI, called per request)
# tesseract-ocr     : local image OCR (the default image engine)
# poppler-utils     : pdf rasterization helpers
# fontconfig+fonts  : LibreOffice needs a font set to avoid blank glyphs
# curl              : healthcheck
#
# Pandoc from the upstream release (Debian's is older); pinned for
# reproducible GFM output.
ARG PANDOC_VERSION=3.9.0.2
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    libreoffice-core \
    tesseract-ocr \
    tesseract-ocr-eng \
    libmagic1 \
    poppler-utils \
    fontconfig \
    fonts-dejavu \
    curl \
    ca-certificates \
    && ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && curl -fsSL -o /tmp/pandoc.deb \
        "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-1-${ARCH}.deb" \
    && dpkg -i /tmp/pandoc.deb \
    && rm /tmp/pandoc.deb \
    && apt-get clean -y \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* \
              /var/cache/apt/archives/*.deb \
    && fc-cache -fv

# --- uv ---------------------------------------------------------------------
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# --- Python deps (locked) ---------------------------------------------------
# Install deps first (cached layer), then the project. Strip markitdown's
# magika + onnxruntime: we dispatch to converters directly and never touch
# markitdown's content-sniffing, so they're dead weight (~200 MB). Stub the
# import sites so `from markitdown import StreamInfo` still succeeds.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev \
    && uv pip uninstall onnxruntime magika 2>/dev/null || true \
    && SITE=$(python -c "import site,sys; sys.stdout.write(site.getsitepackages()[0])") \
    && printf 'class InferenceSession:\n    def __init__(self,*a,**k): raise RuntimeError("onnxruntime stripped")\nclass SessionOptions:\n    pass\n' > "$SITE/onnxruntime.py" \
    && mkdir -p "$SITE/magika" \
    && printf 'class Magika:\n    def __init__(self,*a,**k): raise RuntimeError("magika stripped")\n' > "$SITE/magika/__init__.py"

# --- App code ---------------------------------------------------------------
COPY app/ ./app/
RUN uv sync --frozen --no-dev

# --- Non-root user ----------------------------------------------------------
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
