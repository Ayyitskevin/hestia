import csv
import io

import pytest

from hestia.csv_export import csv_response, safe_cell


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r", "\n"])
def test_safe_cell_neutralizes_spreadsheet_formula_prefixes(prefix):
    assert safe_cell(f"{prefix}payload") == f"'{prefix}payload"


def test_safe_cell_preserves_regular_values_and_stringifies_numbers():
    assert safe_cell("Studio Name") == "Studio Name"
    assert safe_cell(42) == "42"


def test_csv_response_hardens_headers_and_rows_and_sets_attachment_name():
    response = csv_response(
        "studios.csv",
        ["name", "=unsafe-header"],
        [["Aperture Studio", "\n=unsafe-cell"], ["+Formula Studio", 4000]],
    )

    assert response.media_type == "text/csv"
    assert response.headers["content-disposition"] == 'attachment; filename="studios.csv"'
    rows = list(csv.reader(io.StringIO(response.body.decode())))
    assert rows == [
        ["name", "'=unsafe-header"],
        ["Aperture Studio", "'\n=unsafe-cell"],
        ["'+Formula Studio", "4000"],
    ]
