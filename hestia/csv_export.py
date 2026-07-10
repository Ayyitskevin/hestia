"""Shared construction and spreadsheet hardening for CSV downloads."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from fastapi.responses import Response

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def safe_cell(value: object) -> str:
    """Return a CSV cell that spreadsheet applications will treat as literal text."""
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def csv_response(
    filename: str,
    header: Iterable[object],
    rows: Iterable[Iterable[object]],
) -> Response:
    """Build an attachment response while neutralizing every exported cell."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([safe_cell(cell) for cell in header])
    writer.writerows([safe_cell(cell) for cell in row] for row in rows)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
