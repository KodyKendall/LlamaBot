"""OpenPyXL-based read-only analysis tools for the PyXL Excel Analysis Agent.

Ported from the standalone OpenPyXL Excel Analysis Agent. The original used a
single global file path (set via a contextvar per request). In LlamaBot the agent
is conversational and can be pointed at any spreadsheet the user has uploaded, so
every tool now takes an explicit ``file_path`` argument.

Paths are resolved relative to the Rails project root (the shared volume mounted
between LlamaBot and the Rails app), exactly like ``rails_agent``'s ``read_file``.
Uploaded spreadsheets land in ``app/imports/`` (see ``/api/upload-to-assets``), so
a typical ``file_path`` is ``app/imports/sales.xlsx``. Path traversal outside the
Rails root is blocked.
"""
from pathlib import Path

from langchain.tools import tool, ToolRuntime
from langgraph.types import Command
from langchain_core.messages import ToolMessage
import os
import openpyxl
from openpyxl.utils import get_column_letter
from collections import Counter
import statistics
from datetime import datetime, date, time

# Resolve paths against the Rails project root, mirroring rails_agent/tools.py.
# This file lives at app/agents/leonardo/pyxl_agent/tools.py — four parents up is
# the LlamaBot project root, and the Rails code is mounted at app/rails.
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent.parent  # LlamaBot root
RAILS_DIR = PROJECT_ROOT / "app" / "rails"

# Where the upload endpoint drops non-image files (xlsx/xls/csv).
IMPORTS_SUBDIR = "app/imports"

# Spreadsheet extensions the discovery tool surfaces.
SPREADSHEET_EXTENSIONS = {".xlsx", ".xls", ".xlsm", ".csv"}


def _resolve_excel_path(file_path: str) -> str:
    """Resolve a user-supplied path to an absolute path inside the Rails root.

    Accepts a path relative to the Rails root (e.g. ``app/imports/sales.xlsx``) or
    a bare filename (e.g. ``sales.xlsx``), in which case it's looked up under
    ``app/imports/``. Blocks path traversal outside the Rails root.

    Raises ValueError if the path escapes the Rails root or the file is missing.
    """
    cleaned = (file_path or "").strip().lstrip("/")
    if not cleaned:
        raise ValueError("No file_path provided.")

    candidates = [RAILS_DIR / cleaned]
    # If it's a bare filename, also try the imports folder where uploads land.
    if "/" not in cleaned:
        candidates.append(RAILS_DIR / IMPORTS_SUBDIR / cleaned)

    rails_root = RAILS_DIR.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        # Path-traversal guard: must stay within the Rails root.
        if resolved != rails_root and rails_root not in resolved.parents:
            raise ValueError(f"Path '{file_path}' is outside the allowed Rails directory.")
        if resolved.is_file():
            return str(resolved)

    raise ValueError(
        f"File '{file_path}' not found. Uploaded spreadsheets live under "
        f"'{IMPORTS_SUBDIR}/'. Use list_spreadsheets to see what's available."
    )


def _load_workbook(file_path: str):
    """Load the workbook (data_only=False to see formulas)."""
    return openpyxl.load_workbook(_resolve_excel_path(file_path), data_only=False, read_only=True)


def _load_workbook_data(file_path: str):
    """Load the workbook with data_only=True to get computed values."""
    return openpyxl.load_workbook(_resolve_excel_path(file_path), data_only=True, read_only=True)


def _resolve_column(ws, column: str):
    """Resolve a column reference to a 1-based index. Tries header-name match first
    (so short headers like 'ID' or 'AB' aren't mis-resolved as column letters),
    then falls back to Excel column-letter parsing."""
    from openpyxl.utils import column_index_from_string

    # Try header name match first
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
    if header_row:
        target = column.strip().lower()
        for i, h in enumerate(header_row):
            if h is not None and str(h).strip().lower() == target:
                return i + 1

    # Fall back to column letter
    try:
        return column_index_from_string(column)
    except (ValueError, AttributeError):
        return None


def _type_name(value) -> str:
    """Return a human-readable type name for a cell value."""
    if value is None:
        return "empty"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (datetime, date)):
        return "date"
    if isinstance(value, time):
        return "time"
    if isinstance(value, str):
        return "text"
    return type(value).__name__


# =============================================================================
# Tool 0: List Spreadsheets (discovery)
# =============================================================================

@tool("list_spreadsheets")
def list_spreadsheets() -> str:
    """List spreadsheet files the user has uploaded (.xlsx/.xls/.xlsm/.csv under app/imports).

    Call this FIRST to discover which files are available before analyzing. The
    returned paths can be passed directly as the `file_path` argument to the other
    analysis tools.
    """
    try:
        imports_dir = (RAILS_DIR / IMPORTS_SUBDIR).resolve()
        if not imports_dir.is_dir():
            return f"No uploads folder found yet ({IMPORTS_SUBDIR}). Ask the user to attach a spreadsheet."

        files = []
        for f in sorted(imports_dir.iterdir()):
            if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in SPREADSHEET_EXTENSIONS:
                size_kb = f.stat().st_size / 1024
                files.append(f"- {IMPORTS_SUBDIR}/{f.name} ({size_kb:.1f} KB)")

        if not files:
            return f"No spreadsheets found in {IMPORTS_SUBDIR}/. Ask the user to attach a .xlsx, .xls, or .csv file."

        return "Available spreadsheets (pass the path as `file_path`):\n" + "\n".join(files)
    except Exception as e:
        return f"Error listing spreadsheets: {e}"


# =============================================================================
# Tool 1: List Sheets
# =============================================================================

@tool("list_sheets")
def list_sheets(file_path: str) -> str:
    """List all sheets in the Excel workbook with their dimensions (row and column counts).

    Args:
        file_path: Path to the workbook, relative to the Rails root (e.g. 'app/imports/sales.xlsx').
    """
    try:
        wb = _load_workbook_data(file_path)
        results = []
        for name in wb.sheetnames:
            ws = wb[name]
            results.append(f"- {name}: {ws.max_row} rows x {ws.max_column} columns")
        wb.close()
        return "Sheets in workbook:\n" + "\n".join(results)
    except Exception as e:
        return f"Error listing sheets: {e}"


# =============================================================================
# Tool 2: Read Headers
# =============================================================================

@tool("read_headers")
def read_headers(file_path: str, sheet_name: str) -> str:
    """Read the header row (row 1) of a given sheet. Returns column letter, index, and header value.

    Args:
        file_path: Path to the workbook, relative to the Rails root.
        sheet_name: Name of the sheet to read headers from.
    """
    try:
        wb = _load_workbook_data(file_path)
        ws = wb[sheet_name]
        headers = []
        for col_idx, cell in enumerate(next(ws.iter_rows(min_row=1, max_row=1, values_only=False)), 1):
            col_letter = get_column_letter(col_idx)
            headers.append(f"  {col_letter} (col {col_idx}): {cell.value}")
        wb.close()
        return f"Headers for sheet '{sheet_name}':\n" + "\n".join(headers) if headers else f"Sheet '{sheet_name}' appears empty."
    except Exception as e:
        return f"Error reading headers: {e}"


# =============================================================================
# Tool 3: Sample Rows
# =============================================================================

@tool("sample_rows")
def sample_rows(file_path: str, sheet_name: str, start_row: int = 2, num_rows: int = 5) -> str:
    """Read a sample of rows from a sheet. Useful for understanding data content.

    Args:
        file_path: Path to the workbook, relative to the Rails root.
        sheet_name: Name of the sheet to sample from.
        start_row: Row number to start reading from (default: 2, i.e. after headers).
        num_rows: Number of rows to read (default: 5).
    """
    try:
        wb = _load_workbook_data(file_path)
        ws = wb[sheet_name]

        # Get headers first
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        headers = [str(h) if h else f"col_{i+1}" for i, h in enumerate(header_row)] if header_row else []

        lines = [f"Sample rows from '{sheet_name}' (rows {start_row}-{start_row + num_rows - 1}):"]
        if headers:
            lines.append(f"Headers: {headers}")

        row_count = 0
        for row in ws.iter_rows(min_row=start_row, max_row=start_row + num_rows - 1, values_only=True):
            lines.append(f"  Row {start_row + row_count}: {list(row)}")
            row_count += 1

        if row_count == 0:
            lines.append("  (no data rows in this range)")

        wb.close()
        return "\n".join(lines)
    except Exception as e:
        return f"Error sampling rows: {e}"


# =============================================================================
# Tool 4: Get Cell Value
# =============================================================================

@tool("get_cell_value")
def get_cell_value(file_path: str, sheet_name: str, cell_reference: str) -> str:
    """Get the value and type of a specific cell.

    Args:
        file_path: Path to the workbook, relative to the Rails root.
        sheet_name: Name of the sheet.
        cell_reference: Cell reference like 'B5' or 'AA12'.
    """
    try:
        resolved = _resolve_excel_path(file_path)
        # Use non-read-only mode for cell reference access
        wb = openpyxl.load_workbook(resolved, data_only=False)
        ws = wb[sheet_name]
        cell = ws[cell_reference]
        value = cell.value
        data_type = _type_name(value)

        # Check if it's a formula
        is_formula = isinstance(value, str) and value.startswith("=")

        result = f"Cell {sheet_name}!{cell_reference}:\n  Value: {value}\n  Type: {data_type}"
        if is_formula:
            # Also get computed value
            wb2 = openpyxl.load_workbook(resolved, data_only=True)
            ws2 = wb2[sheet_name]
            computed = ws2[cell_reference].value
            wb2.close()
            result += f"\n  Formula: {value}\n  Computed value: {computed}"

        wb.close()
        return result
    except Exception as e:
        return f"Error reading cell: {e}"


# =============================================================================
# Tool 5: Get Sheet Dimensions
# =============================================================================

@tool("get_sheet_dimensions")
def get_sheet_dimensions(file_path: str, sheet_name: str) -> str:
    """Get detailed dimensions and data density info for a sheet.

    Args:
        file_path: Path to the workbook, relative to the Rails root.
        sheet_name: Name of the sheet.
    """
    try:
        wb = _load_workbook_data(file_path)
        ws = wb[sheet_name]
        max_row = ws.max_row or 0
        max_col = ws.max_column or 0
        total_cells = max_row * max_col

        # Count non-empty cells by sampling
        non_empty = 0
        sampled = 0
        for row in ws.iter_rows(values_only=True):
            for val in row:
                sampled += 1
                if val is not None:
                    non_empty += 1
            if sampled > 50000:
                break

        density = (non_empty / sampled * 100) if sampled > 0 else 0

        wb.close()
        return (
            f"Sheet '{sheet_name}' dimensions:\n"
            f"  Rows: {max_row}\n"
            f"  Columns: {max_col}\n"
            f"  Total cells: {total_cells}\n"
            f"  Data density: {density:.1f}% (sampled {sampled} cells, {non_empty} non-empty)"
        )
    except Exception as e:
        return f"Error getting dimensions: {e}"


# =============================================================================
# Tool 6: Summarize Column
# =============================================================================

@tool("summarize_column")
def summarize_column(file_path: str, sheet_name: str, column: str) -> str:
    """Summarize a single column: type breakdown, nulls, uniques, top values, and numeric stats.

    Args:
        file_path: Path to the workbook, relative to the Rails root.
        sheet_name: Name of the sheet.
        column: Column letter (e.g. 'A', 'B', 'AA') or column header name.
    """
    try:
        wb = _load_workbook_data(file_path)
        ws = wb[sheet_name]

        col_idx = _resolve_column(ws, column)
        if col_idx is None:
            wb.close()
            return f"Could not resolve column '{column}'. Use a column letter (A, B, ...) or exact header name."

        # Read all values in the column (skip header), cap at 10000
        values = []
        for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx, values_only=True):
            values.append(row[0])
            if len(values) >= 10000:
                break

        total = len(values)
        nulls = sum(1 for v in values if v is None)
        non_null = [v for v in values if v is not None]

        # Type breakdown
        type_counts = Counter(_type_name(v) for v in values)
        type_str = ", ".join(f"{t}: {c}" for t, c in type_counts.most_common())

        # Unique values
        unique_count = len(set(str(v) for v in non_null))

        # Top values
        value_counts = Counter(str(v) for v in non_null)
        top_values = value_counts.most_common(10)
        top_str = "\n".join(f"    {val}: {cnt} ({cnt/total*100:.1f}%)" for val, cnt in top_values)

        lines = [
            f"Column '{column}' summary (sheet '{sheet_name}'):",
            f"  Total rows: {total}",
            f"  Null/empty: {nulls} ({nulls/total*100:.1f}%)" if total > 0 else "  Null/empty: 0",
            f"  Unique values: {unique_count}",
            f"  Type breakdown: {type_str}",
            f"  Top 10 values:\n{top_str}",
        ]

        # Numeric stats if applicable
        numbers = [v for v in non_null if isinstance(v, (int, float))]
        if numbers:
            lines.append(f"  Numeric stats ({len(numbers)} values):")
            lines.append(f"    Min: {min(numbers)}")
            lines.append(f"    Max: {max(numbers)}")
            lines.append(f"    Mean: {statistics.mean(numbers):.4f}")
            lines.append(f"    Median: {statistics.median(numbers):.4f}")
            if len(numbers) >= 2:
                lines.append(f"    Std Dev: {statistics.stdev(numbers):.4f}")

        wb.close()
        return "\n".join(lines)
    except Exception as e:
        return f"Error summarizing column: {e}"


# =============================================================================
# Tool 7: Detect Column Types
# =============================================================================

@tool("detect_column_types")
def detect_column_types(file_path: str, sheet_name: str) -> str:
    """Detect the inferred data type for each column in a sheet based on sampling.

    Args:
        file_path: Path to the workbook, relative to the Rails root.
        sheet_name: Name of the sheet.
    """
    try:
        wb = _load_workbook_data(file_path)
        ws = wb[sheet_name]

        # Get headers
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        max_col = ws.max_column or 0

        # Sample up to 100 rows
        sample_data = []
        for row in ws.iter_rows(min_row=2, max_row=101, values_only=True):
            sample_data.append(row)

        results = []
        for col_idx in range(max_col):
            header = header_row[col_idx] if header_row and col_idx < len(header_row) else f"col_{col_idx+1}"
            col_letter = get_column_letter(col_idx + 1)

            col_values = [row[col_idx] for row in sample_data if col_idx < len(row)]
            non_null = [v for v in col_values if v is not None]

            if not non_null:
                inferred = "empty"
            else:
                types = set(_type_name(v) for v in non_null)
                if len(types) == 1:
                    inferred = types.pop()
                elif types == {"number"}:
                    inferred = "number"
                elif "date" in types and len(types) <= 2:
                    inferred = "date (mixed)"
                else:
                    inferred = f"mixed ({', '.join(sorted(types))})"

            null_count = len(col_values) - len(non_null)
            results.append(f"  {col_letter} ({header}): {inferred} [{null_count} nulls in sample]")

        wb.close()
        return f"Column types for sheet '{sheet_name}' (sampled {len(sample_data)} rows):\n" + "\n".join(results)
    except Exception as e:
        return f"Error detecting column types: {e}"


# =============================================================================
# Tool 8: Find Patterns and Anomalies
# =============================================================================

@tool("find_patterns_and_anomalies")
def find_patterns_and_anomalies(file_path: str, sheet_name: str) -> str:
    """Scan a sheet for duplicate rows, empty rows/columns, constant columns, high-null columns, and numeric outliers.

    Args:
        file_path: Path to the workbook, relative to the Rails root.
        sheet_name: Name of the sheet.
    """
    try:
        wb = _load_workbook_data(file_path)
        ws = wb[sheet_name]
        max_col = ws.max_column or 0

        all_rows = []
        col_values = {i: [] for i in range(max_col)}

        for row in ws.iter_rows(min_row=2, values_only=True):
            all_rows.append(tuple(row))
            for i in range(min(len(row), max_col)):
                col_values[i].append(row[i])
            if len(all_rows) >= 10000:
                break

        findings = [f"Patterns & anomalies for sheet '{sheet_name}' ({len(all_rows)} rows scanned):"]

        # Duplicate rows
        row_counts = Counter(all_rows)
        duplicates = {k: v for k, v in row_counts.items() if v > 1}
        findings.append(f"\n  Duplicate rows: {len(duplicates)} unique rows appear more than once ({sum(v - 1 for v in duplicates.values())} total extra rows)")

        # Empty rows
        empty_rows = sum(1 for row in all_rows if all(v is None for v in row))
        findings.append(f"  Completely empty rows: {empty_rows}")

        # Per-column analysis
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=False), None)
        empty_cols = []
        constant_cols = []
        high_null_cols = []
        outlier_cols = []

        for i in range(max_col):
            vals = col_values[i]
            header = header_row[i].value if header_row and i < len(header_row) else f"col_{i+1}"
            col_letter = get_column_letter(i + 1)
            label = f"{col_letter} ({header})"

            non_null = [v for v in vals if v is not None]
            null_rate = (len(vals) - len(non_null)) / len(vals) if vals else 0

            if not non_null:
                empty_cols.append(label)
                continue

            if null_rate > 0.5:
                high_null_cols.append(f"{label}: {null_rate*100:.0f}% null")

            if len(set(str(v) for v in non_null)) == 1:
                constant_cols.append(f"{label}: always '{non_null[0]}'")

            # Numeric outliers (>3 stdev)
            numbers = [v for v in non_null if isinstance(v, (int, float))]
            if len(numbers) >= 10:
                mean = statistics.mean(numbers)
                stdev = statistics.stdev(numbers)
                if stdev > 0:
                    outliers = [v for v in numbers if abs(v - mean) > 3 * stdev]
                    if outliers:
                        outlier_cols.append(f"{label}: {len(outliers)} outliers (>{3}σ from mean {mean:.2f})")

        if empty_cols:
            findings.append(f"\n  Empty columns: {', '.join(empty_cols)}")
        if constant_cols:
            findings.append(f"\n  Constant columns:\n    " + "\n    ".join(constant_cols))
        if high_null_cols:
            findings.append(f"\n  High-null columns (>50%):\n    " + "\n    ".join(high_null_cols))
        if outlier_cols:
            findings.append(f"\n  Columns with outliers:\n    " + "\n    ".join(outlier_cols))

        if not (empty_cols or constant_cols or high_null_cols or outlier_cols or duplicates or empty_rows):
            findings.append("\n  No significant anomalies detected.")

        wb.close()
        return "\n".join(findings)
    except Exception as e:
        return f"Error finding patterns: {e}"


# =============================================================================
# Tool 9: Statistical Analysis
# =============================================================================

@tool("statistical_analysis")
def statistical_analysis(file_path: str, sheet_name: str, column: str) -> str:
    """Perform detailed statistical analysis on a numeric column: count, mean, median, mode, stdev, quartiles, histogram.

    Args:
        file_path: Path to the workbook, relative to the Rails root.
        sheet_name: Name of the sheet.
        column: Column letter (e.g. 'B') or header name.
    """
    try:
        wb = _load_workbook_data(file_path)
        ws = wb[sheet_name]

        col_idx = _resolve_column(ws, column)
        if col_idx is None:
            wb.close()
            return f"Could not resolve column '{column}'."

        # Read numeric values
        numbers = []
        non_numeric = 0
        for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx, values_only=True):
            val = row[0]
            if val is None:
                continue
            if isinstance(val, (int, float)):
                numbers.append(float(val))
            else:
                non_numeric += 1
            if len(numbers) >= 10000:
                break

        if not numbers:
            wb.close()
            return f"No numeric values found in column '{column}'."

        numbers.sort()
        n = len(numbers)

        # Quartiles
        q1 = numbers[n // 4] if n >= 4 else numbers[0]
        q2 = statistics.median(numbers)
        q3 = numbers[3 * n // 4] if n >= 4 else numbers[-1]
        iqr = q3 - q1

        # Mode
        try:
            mode_val = statistics.mode(numbers)
        except statistics.StatisticsError:
            mode_val = "no unique mode"

        # Histogram (10 buckets)
        min_val, max_val = numbers[0], numbers[-1]
        if min_val == max_val:
            histogram = f"    All values are {min_val}"
        else:
            bucket_size = (max_val - min_val) / 10
            buckets = [0] * 10
            for v in numbers:
                idx = min(int((v - min_val) / bucket_size), 9)
                buckets[idx] += 1
            hist_lines = []
            for i, count in enumerate(buckets):
                lo = min_val + i * bucket_size
                hi = lo + bucket_size
                bar = "#" * min(count * 50 // max(buckets), 50) if max(buckets) > 0 else ""
                hist_lines.append(f"    [{lo:>10.2f} - {hi:>10.2f}]: {count:>5} {bar}")
            histogram = "\n".join(hist_lines)

        lines = [
            f"Statistical analysis for column '{column}' (sheet '{sheet_name}'):",
            f"  Count: {n}",
            f"  Non-numeric skipped: {non_numeric}",
            f"  Min: {min_val}",
            f"  Max: {max_val}",
            f"  Mean: {statistics.mean(numbers):.4f}",
            f"  Median: {q2:.4f}",
            f"  Mode: {mode_val}",
            f"  Std Dev: {statistics.stdev(numbers):.4f}" if n >= 2 else "  Std Dev: N/A (need ≥2 values)",
            f"  Q1 (25th): {q1}",
            f"  Q3 (75th): {q3}",
            f"  IQR: {iqr}",
            f"  Distribution (10 buckets):\n{histogram}",
        ]

        wb.close()
        return "\n".join(lines)
    except Exception as e:
        return f"Error in statistical analysis: {e}"


# =============================================================================
# Tool 10: Check Formulas
# =============================================================================

@tool("check_formulas")
def check_formulas(file_path: str, sheet_name: str) -> str:
    """Scan a sheet for cells containing formulas and return their locations and formula strings.

    Args:
        file_path: Path to the workbook, relative to the Rails root.
        sheet_name: Name of the sheet.
    """
    try:
        wb = openpyxl.load_workbook(_resolve_excel_path(file_path), data_only=False, read_only=True)
        ws = wb[sheet_name]

        formulas = []
        row_count = 0
        for row in ws.iter_rows(values_only=False):
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    formulas.append(f"  {cell.coordinate}: {cell.value}")
            row_count += 1
            if row_count >= 5000:
                break
            if len(formulas) >= 200:
                formulas.append(f"  ... (stopped after 200 formulas, scanned {row_count} rows)")
                break

        wb.close()

        if not formulas:
            return f"No formulas found in sheet '{sheet_name}' (scanned {row_count} rows)."

        return f"Formulas in sheet '{sheet_name}' ({len(formulas)} found):\n" + "\n".join(formulas)
    except Exception as e:
        return f"Error checking formulas: {e}"


# =============================================================================
# Tool 11: Find Cross-Sheet Relationships
# =============================================================================

@tool("find_cross_sheet_relationships")
def find_cross_sheet_relationships(file_path: str) -> str:
    """Compare header names across all sheets to identify shared columns that might be join keys.

    Args:
        file_path: Path to the workbook, relative to the Rails root.
    """
    try:
        wb = _load_workbook_data(file_path)

        sheet_headers = {}
        for name in wb.sheetnames:
            ws = wb[name]
            header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
            if header_row:
                headers = [str(h).strip().lower() for h in header_row if h is not None]
                sheet_headers[name] = headers

        wb.close()

        if len(sheet_headers) < 2:
            return "Only one sheet with headers found -- no cross-sheet relationships to analyze."

        # Find shared headers
        sheet_names = list(sheet_headers.keys())
        relationships = []
        for i in range(len(sheet_names)):
            for j in range(i + 1, len(sheet_names)):
                s1, s2 = sheet_names[i], sheet_names[j]
                shared = set(sheet_headers[s1]) & set(sheet_headers[s2])
                if shared:
                    relationships.append(f"  '{s1}' <-> '{s2}': shared columns = {sorted(shared)}")

        if not relationships:
            return "No shared column names found between sheets."

        return "Cross-sheet relationships (shared column names):\n" + "\n".join(relationships)
    except Exception as e:
        return f"Error finding relationships: {e}"


# =============================================================================
# Tool 12: Data Quality Check
# =============================================================================

@tool("data_quality_check")
def data_quality_check(file_path: str, sheet_name: str) -> str:
    """Check data quality: missing values per column, duplicate rows, whitespace issues, mixed types.

    Args:
        file_path: Path to the workbook, relative to the Rails root.
        sheet_name: Name of the sheet.
    """
    try:
        wb = _load_workbook_data(file_path)
        ws = wb[sheet_name]
        max_col = ws.max_column or 0

        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)

        # Collect data
        all_rows = []
        col_data = {i: [] for i in range(max_col)}

        for row in ws.iter_rows(min_row=2, values_only=True):
            all_rows.append(tuple(row))
            for i in range(min(len(row), max_col)):
                col_data[i].append(row[i])
            if len(all_rows) >= 10000:
                break

        total_rows = len(all_rows)
        lines = [f"Data quality report for sheet '{sheet_name}' ({total_rows} rows scanned):"]

        # Missing values per column
        lines.append("\n  Missing values per column:")
        for i in range(max_col):
            header = header_row[i] if header_row and i < len(header_row) else f"col_{i+1}"
            col_letter = get_column_letter(i + 1)
            nulls = sum(1 for v in col_data[i] if v is None)
            pct = nulls / total_rows * 100 if total_rows > 0 else 0
            if nulls > 0:
                lines.append(f"    {col_letter} ({header}): {nulls} ({pct:.1f}%)")
        if not any(sum(1 for v in col_data[i] if v is None) > 0 for i in range(max_col)):
            lines.append("    No missing values found.")

        # Duplicate rows
        row_counts = Counter(all_rows)
        dup_count = sum(1 for v in row_counts.values() if v > 1)
        extra_rows = sum(v - 1 for v in row_counts.values() if v > 1)
        lines.append(f"\n  Duplicate rows: {dup_count} unique rows duplicated ({extra_rows} extra rows)")

        # Whitespace issues in text columns
        whitespace_issues = []
        for i in range(max_col):
            header = header_row[i] if header_row and i < len(header_row) else f"col_{i+1}"
            col_letter = get_column_letter(i + 1)
            text_vals = [v for v in col_data[i] if isinstance(v, str)]
            leading_trailing = sum(1 for v in text_vals if v != v.strip())
            if leading_trailing > 0:
                whitespace_issues.append(f"    {col_letter} ({header}): {leading_trailing} values with leading/trailing whitespace")

        if whitespace_issues:
            lines.append("\n  Whitespace issues:")
            lines.extend(whitespace_issues)

        # Mixed types per column
        mixed = []
        for i in range(max_col):
            header = header_row[i] if header_row and i < len(header_row) else f"col_{i+1}"
            col_letter = get_column_letter(i + 1)
            non_null = [v for v in col_data[i] if v is not None]
            types = set(_type_name(v) for v in non_null)
            if len(types) > 1:
                type_counts = Counter(_type_name(v) for v in non_null)
                breakdown = ", ".join(f"{t}: {c}" for t, c in type_counts.most_common())
                mixed.append(f"    {col_letter} ({header}): {breakdown}")

        if mixed:
            lines.append("\n  Mixed-type columns:")
            lines.extend(mixed)

        wb.close()
        return "\n".join(lines)
    except Exception as e:
        return f"Error in data quality check: {e}"


# ---------------------------------------------------------------------------
# Tech-spec file tools
# ---------------------------------------------------------------------------

TECH_SPECS_SUBDIR = "app/imports/tech_specs"
UBUNTU_UID = 1000
UBUNTU_GID = 1000


def _chown_for_ubuntu(path: Path) -> None:
    """Best-effort chown so the host ubuntu user can access the file."""
    try:
        os.chown(path, UBUNTU_UID, UBUNTU_GID)
    except (OSError, PermissionError):
        pass


@tool("read_tech_spec")
def read_tech_spec(spreadsheet_name: str) -> str:
    """Check if a tech spec already exists for a spreadsheet and return its contents.

    Args:
        spreadsheet_name: The spreadsheet filename (e.g. 'sales.xlsx').
                          Looks for app/imports/tech_specs/TECH_SPEC_<stem>.md.
    """
    stem = Path(spreadsheet_name).stem
    spec_path = RAILS_DIR / TECH_SPECS_SUBDIR / f"TECH_SPEC_{stem}.md"

    if not spec_path.exists():
        return (
            f"No existing tech spec found for '{spreadsheet_name}'. "
            f"Path checked: {TECH_SPECS_SUBDIR}/TECH_SPEC_{stem}.md"
        )

    try:
        content = spec_path.read_text()
        if not content.strip():
            return f"Tech spec file exists but is empty: {TECH_SPECS_SUBDIR}/TECH_SPEC_{stem}.md"
        return f"Existing tech spec found ({TECH_SPECS_SUBDIR}/TECH_SPEC_{stem}.md):\n\n{content}"
    except Exception as e:
        return f"Error reading tech spec: {e}"


@tool("write_tech_spec")
def write_tech_spec(
    spreadsheet_name: str,
    content: str,
    runtime: ToolRuntime,
) -> Command:
    """Save the final tech spec as a markdown file.

    Args:
        spreadsheet_name: The spreadsheet filename this spec is for (e.g. 'sales.xlsx').
        content: The full markdown content of the tech spec.
    """
    stem = Path(spreadsheet_name).stem
    spec_dir = RAILS_DIR / TECH_SPECS_SUBDIR
    spec_path = spec_dir / f"TECH_SPEC_{stem}.md"

    try:
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(content)
        _chown_for_ubuntu(spec_path)
    except Exception as e:
        error_msg = f"Error writing tech spec: {e}"
        return Command(update={"messages": [
            ToolMessage(error_msg, artifact={"status": "error", "message": error_msg}, tool_call_id=runtime.tool_call_id)
        ]})

    rel_path = f"{TECH_SPECS_SUBDIR}/TECH_SPEC_{stem}.md"
    success_msg = f"Tech spec saved to {rel_path}"
    return Command(update={"messages": [
        ToolMessage(success_msg, artifact={"status": "success", "message": success_msg, "file_path": rel_path}, tool_call_id=runtime.tool_call_id)
    ]})
