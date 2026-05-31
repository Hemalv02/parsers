"""parser-service: multi-format document → markdown parser for RAG ingestion.

Public surface:
  - `app`        the FastAPI application (`app.main:app`)
  - `dispatch`   the extension-routing function used by both API and CLI
  - `cli.main`   the `parser-service` console entrypoint
"""

__version__ = "0.1.0"

from .cli import main

__all__ = ["main", "__version__"]
