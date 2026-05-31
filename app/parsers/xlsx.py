"""XLSX/XLSM → markdown (markitdown) + structured sheets (pandas).

markdown keeps markitdown's clean per-sheet tables. The structured form is
`{"sheets": [{"name", "rows": [[cell, ...], ...]}]}` via pandas, capped at
`settings.max_rows_per_sheet` rows/sheet (mirrors the backend) so a giant
workbook can't balloon the JSON response.
"""

from __future__ import annotations

from pathlib import Path

from markitdown.converters import XlsxConverter

from ..config import settings
from .base import BaseParser, ParseResult
from .markitdown_util import convert_with_markitdown


class XlsxParser(BaseParser):
    name = "markitdown-xlsx"
    extensions = (".xlsx", ".xlsm")
    isolation = True

    def parse(self, path: Path, mode: str) -> ParseResult:
        md = convert_with_markitdown(XlsxConverter, ".xlsx", path)
        structured = self._structured(path) if self.wants_structured(mode) else None
        return ParseResult(parser=self.name, markdown=md, structured=structured)

    def _structured(self, path: Path) -> dict:
        import pandas as pd

        cap = settings.max_rows_per_sheet
        sheets = []
        frames = pd.read_excel(path, sheet_name=None, header=None)
        for name, df in frames.items():
            truncated = len(df) > cap
            df = df.head(cap).fillna("")
            rows = df.astype(str).values.tolist()
            sheets.append({"name": name, "rows": rows, "truncated": truncated})
        return {"sheets": sheets}
