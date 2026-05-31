"""PDF helpers vendored from markitdown's `_pdf_converter.py`.

Source: https://github.com/microsoft/markitdown/blob/main/packages/markitdown/src/markitdown/converters/_pdf_converter.py
License: MIT (Microsoft Corporation)

We vendor instead of importing from `markitdown.converters._pdf_converter`
for two reasons:

  1. Stability — those names start with `_`, signalling private API.
     Vendoring pins behavior across markitdown upgrades.
  2. Citations — markitdown's `PdfConverter.convert()` discards per-page
     chunks and returns a single pdfminer blob when most pages are plain
     text. We need per-page boundaries for RAG citation, so we use only
     markitdown's form-detection helpers and run our own per-page loop.

Vendored functions (verbatim, just stripped of `Any` re-imports):
  - `_merge_partial_numbering_lines(text)`
  - `_extract_form_content_from_words(page)`

Everything else (the PdfConverter class, the whole-doc fallback, etc.) is
re-implemented in `converter_worker.py`.

If you upgrade or modify these helpers, keep the MIT notice intact.
"""

from __future__ import annotations

import re
from typing import Any

# Pattern for MasterFormat-style partial numbering (e.g., ".1", ".2", ".10")
PARTIAL_NUMBERING_PATTERN = re.compile(r"^\.\d+$")


def merge_partial_numbering_lines(text: str) -> str:
    """Merge MasterFormat-style partial numbering with the following line.

    Some PDFs split `.1  The intent of...` across two lines (just `.1`
    on its own, then the prose). This stitches them back together.
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if PARTIAL_NUMBERING_PATTERN.match(stripped):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                next_line = lines[j].strip()
                result_lines.append(f"{stripped} {next_line}")
                i = j + 1
            else:
                result_lines.append(line)
                i += 1
        else:
            result_lines.append(line)
            i += 1
    return "\n".join(result_lines)


def extract_form_content_from_words(page: Any) -> str | None:
    """Form-style detection. Returns markdown table for form pages, else None.

    Verbatim vendor from markitdown 0.1.x. See the source for the
    column-clustering heuristic; rough summary:

      - Group words by Y position (rows).
      - Find global column boundaries from rows with 3+ x-groups.
      - Classify each row as table-row if it aligns with 2+ global columns.
      - If <20% of rows are table rows, return None (use plain text instead).
      - Otherwise emit pipe-aligned markdown table(s) interleaved with
        any non-table text from the page.
    """
    words = page.extract_words(keep_blank_chars=True, x_tolerance=3, y_tolerance=3)
    if not words:
        return None

    y_tolerance = 5
    rows_by_y: dict[float, list[dict]] = {}
    for word in words:
        y_key = round(word["top"] / y_tolerance) * y_tolerance
        rows_by_y.setdefault(y_key, []).append(word)

    sorted_y_keys = sorted(rows_by_y.keys())
    page_width = page.width if hasattr(page, "width") else 612

    row_info: list[dict] = []
    for y_key in sorted_y_keys:
        row_words = sorted(rows_by_y[y_key], key=lambda w: w["x0"])
        if not row_words:
            continue
        first_x0 = row_words[0]["x0"]
        last_x1 = row_words[-1]["x1"]
        line_width = last_x1 - first_x0
        combined_text = " ".join(w["text"] for w in row_words)
        x_positions = [w["x0"] for w in row_words]
        x_groups: list[float] = []
        for x in sorted(x_positions):
            if not x_groups or x - x_groups[-1] > 50:
                x_groups.append(x)
        is_paragraph = line_width > page_width * 0.55 and len(combined_text) > 60
        has_partial_numbering = False
        if row_words:
            first_word = row_words[0]["text"].strip()
            if PARTIAL_NUMBERING_PATTERN.match(first_word):
                has_partial_numbering = True
        row_info.append(
            {
                "y_key": y_key,
                "words": row_words,
                "text": combined_text,
                "x_groups": x_groups,
                "is_paragraph": is_paragraph,
                "num_columns": len(x_groups),
                "has_partial_numbering": has_partial_numbering,
            }
        )

    all_table_x_positions: list[float] = []
    for info in row_info:
        if info["num_columns"] >= 3 and not info["is_paragraph"]:
            all_table_x_positions.extend(info["x_groups"])
    if not all_table_x_positions:
        return None

    all_table_x_positions.sort()
    gaps = []
    for i in range(len(all_table_x_positions) - 1):
        gap = all_table_x_positions[i + 1] - all_table_x_positions[i]
        if gap > 5:
            gaps.append(gap)

    if gaps and len(gaps) >= 3:
        sorted_gaps = sorted(gaps)
        adaptive_tolerance = sorted_gaps[int(len(sorted_gaps) * 0.70)]
        adaptive_tolerance = max(25, min(50, adaptive_tolerance))
    else:
        adaptive_tolerance = 35

    global_columns: list[float] = []
    for x in all_table_x_positions:
        if not global_columns or x - global_columns[-1] > adaptive_tolerance:
            global_columns.append(x)

    if len(global_columns) > 1:
        content_width = global_columns[-1] - global_columns[0]
        avg_col_width = content_width / len(global_columns)
        if avg_col_width < 30:
            return None
        columns_per_inch = len(global_columns) / (content_width / 72)
        if columns_per_inch > 10:
            return None
        adaptive_max_columns = max(15, int(20 * (page_width / 612)))
        if len(global_columns) > adaptive_max_columns:
            return None
    else:
        return None

    for info in row_info:
        if info["is_paragraph"]:
            info["is_table_row"] = False
            continue
        if info["has_partial_numbering"]:
            info["is_table_row"] = False
            continue
        aligned_columns: set[int] = set()
        for word in info["words"]:
            word_x = word["x0"]
            for col_idx, col_x in enumerate(global_columns):
                if abs(word_x - col_x) < 40:
                    aligned_columns.add(col_idx)
                    break
        info["is_table_row"] = len(aligned_columns) >= 2

    table_regions: list[tuple[int, int]] = []
    i = 0
    while i < len(row_info):
        if row_info[i]["is_table_row"]:
            start_idx = i
            while i < len(row_info) and row_info[i]["is_table_row"]:
                i += 1
            table_regions.append((start_idx, i))
        else:
            i += 1

    total_table_rows = sum(end - start for start, end in table_regions)
    if len(row_info) > 0 and total_table_rows / len(row_info) < 0.2:
        return None

    result_lines: list[str] = []
    num_cols = len(global_columns)

    def extract_cells(info: dict) -> list[str]:
        cells: list[str] = ["" for _ in range(num_cols)]
        for word in info["words"]:
            word_x = word["x0"]
            assigned_col = num_cols - 1
            for col_idx in range(num_cols - 1):
                col_end = global_columns[col_idx + 1]
                if word_x < col_end - 20:
                    assigned_col = col_idx
                    break
            if cells[assigned_col]:
                cells[assigned_col] += " " + word["text"]
            else:
                cells[assigned_col] = word["text"]
        return cells

    idx = 0
    while idx < len(row_info):
        info = row_info[idx]
        table_region = None
        for start, end in table_regions:
            if idx == start:
                table_region = (start, end)
                break
        if table_region:
            start, end = table_region
            table_data: list[list[str]] = []
            for table_idx in range(start, end):
                table_data.append(extract_cells(row_info[table_idx]))
            if table_data:
                col_widths = [max(len(row[col]) for row in table_data) for col in range(num_cols)]
                col_widths = [max(w, 3) for w in col_widths]
                header = table_data[0]
                header_str = (
                    "| "
                    + " | ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(header))
                    + " |"
                )
                result_lines.append(header_str)
                separator = "| " + " | ".join("-" * col_widths[i] for i in range(num_cols)) + " |"
                result_lines.append(separator)
                for row in table_data[1:]:
                    row_str = (
                        "| "
                        + " | ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(row))
                        + " |"
                    )
                    result_lines.append(row_str)
            idx = end
        else:
            in_table = False
            for start, end in table_regions:
                if start < idx < end:
                    in_table = True
                    break
            if not in_table:
                result_lines.append(info["text"])
            idx += 1

    return "\n".join(result_lines)
