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
from ..metadata import ooxml_core_props, xlsx_sheet_count
from .base import BaseParser, ParseResult
from .markitdown_util import convert_with_markitdown


class XlsxParser(BaseParser):
    name = "markitdown-xlsx"
    extensions = (".xlsx", ".xlsm")
    isolation = True

    def parse(self, path: Path, mode: str) -> ParseResult:
        md = convert_with_markitdown(XlsxConverter, ".xlsx", path)
        structured = self._structured(path) if self.wants_structured(mode) else None
        metadata = ooxml_core_props(path)
        sheets = xlsx_sheet_count(path)
        if sheets is not None:
            metadata["sheet_count"] = sheets
        # Sheet names need the workbook parsed; surface them only when we
        # already paid for the structured pass.
        if structured:
            metadata["sheet_names"] = [s["name"] for s in structured["sheets"]]
        return ParseResult(parser=self.name, markdown=md, structured=structured, metadata=metadata)

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
