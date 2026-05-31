"""CSV/TSV → markdown table (pandas, markitdown fallback) + structured rows.

Failure modes are non-overlapping across the two libs (verified on a
1344-file benchmark): pandas fails on inconsistent column counts and on
multi-MB-row tail-latency; markitdown fails on huge single fields (>131KB)
and newlines-in-unquoted-fields. Trying pandas first keeps typical-case
latency low (~50ms vs ~500ms); the fallback catches files that would
otherwise 500. The parser label reflects which engine won.
"""

from __future__ import annotations

from pathlib import Path

from markitdown.converters import CsvConverter

from ..config import settings
from .base import BaseParser, ParseResult
from .markitdown_util import convert_with_markitdown


class CsvParser(BaseParser):
    name = "csv"
    extensions = (".csv", ".tsv")
    isolation = True

    def parse(self, path: Path, mode: str) -> ParseResult:
        md, used = self._to_markdown(path)
        structured = self._structured(path) if self.wants_structured(mode) else None
        return ParseResult(parser=used, markdown=md, structured=structured)

    def _to_markdown(self, path: Path) -> tuple[str, str]:
        import pandas as pd

        sep = "\t" if path.suffix.lower() == ".tsv" else ","
        # 1. Fast path: pandas, utf-8.
        try:
            df = pd.read_csv(path, sep=sep, low_memory=False)
            return df.to_markdown(index=False), "pandas"
        except (pd.errors.ParserError, MemoryError):
            pass
        except UnicodeDecodeError:
            # 2. pandas with latin-1 (cp1252-ish files that aren't valid utf-8).
            try:
                df = pd.read_csv(path, sep=sep, encoding="latin-1", low_memory=False)
                return df.to_markdown(index=False), "pandas-latin1"
            except (pd.errors.ParserError, MemoryError):
                pass
        # 3. Final fallback: markitdown's CsvConverter (Python csv module),
        #    which catches malformed-row issues pandas couldn't.
        md = convert_with_markitdown(CsvConverter, path.suffix.lower(), path)
        return md, "markitdown"

    def _structured(self, path: Path) -> dict:
        import pandas as pd

        sep = "\t" if path.suffix.lower() == ".tsv" else ","
        cap = settings.max_rows_per_sheet

        # Mirror the markdown path's fallback ladder so structured mode never
        # 500s where markdown would have succeeded: pandas utf-8 -> latin-1,
        # then the stdlib csv reader (tolerant of ragged/malformed rows that
        # trip pandas' ParserError).
        df = None
        for enc in ("utf-8", "latin-1"):
            try:
                df = pd.read_csv(path, sep=sep, encoding=enc, low_memory=False)
                break
            except UnicodeDecodeError:
                continue
            except pd.errors.ParserError:
                break  # malformed for pandas — drop to the stdlib reader

        if df is not None:
            truncated = len(df) > cap
            df = df.head(cap).fillna("")
            return {
                "columns": [str(c) for c in df.columns],
                "rows": df.astype(str).values.tolist(),
                "truncated": truncated,
            }

        # stdlib csv fallback — reads ragged rows the C parser rejects.
        import csv
        from itertools import islice

        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh, delimiter=sep)
            all_rows = list(islice(reader, cap + 2))  # header + cap rows + 1 to detect truncation
        columns = all_rows[0] if all_rows else []
        rows = all_rows[1 : cap + 1]
        return {"columns": columns, "rows": rows, "truncated": len(all_rows) > cap + 1}
