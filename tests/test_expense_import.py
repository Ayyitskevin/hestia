"""Expense CSV import — parsing, magnitude/dedup/idempotency, tenant scoping, HTTP flow."""

from conftest import login_owner, onboard_studio

from hestia.db import connect
from hestia.finances import create_expense, import_expenses, list_expenses
from hestia.routes.finances import _parse_expense_csv
from hestia.tenants import create_tenant


def _tenant(conn, name="Expense Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


# ── CSV parsing ───────────────────────────────────────────────────────────────


def test_parse_header_any_order_with_synonyms():
    text = "Memo,Transaction Date,Debit\nCoffee,2026-01-03,5.00\n"
    rows = _parse_expense_csv(text)
    assert rows == [{"incurred_on": "2026-01-03", "category": "", "description": "Coffee",
                     "amount_cents": 500}]


def test_parse_positional_without_header():
    rows = _parse_expense_csv("2026-01-03,Coffee,5.00\n")
    assert rows[0]["incurred_on"] == "2026-01-03" and rows[0]["description"] == "Coffee"
    assert rows[0]["amount_cents"] == 500


def test_parse_amount_is_magnitude_and_overflow_safe():
    rows = _parse_expense_csv("date,amount,description\n2026-01-03,-12.50,Gas\n2026-01-04,1e308,Big\n")
    assert rows[0]["amount_cents"] == 1250                     # negative debit → magnitude
    assert rows[1]["amount_cents"] == 0                        # overflow floors to 0


def test_parse_skips_blank_lines():
    rows = _parse_expense_csv("amount\n\n9.99\n")
    assert len(rows) == 1 and rows[0]["amount_cents"] == 999


def test_parse_numeric_header_label_still_detected_as_header():
    # a numeric column label (e.g. a year) must NOT defeat header detection → no data loss
    text = "date,amount,description,2026\n2026-01-03,5.00,Coffee,Q1\n2026-02-04,12.50,Lunch,Q1\n"
    rows = _parse_expense_csv(text)
    assert [r["amount_cents"] for r in rows] == [500, 1250]
    assert [r["description"] for r in rows] == ["Coffee", "Lunch"]


def test_parse_recognizes_debit_side_bank_labels():
    rows = _parse_expense_csv("date,withdrawal,description\n2026-01-03,5.00,Coffee\n")
    assert rows[0]["amount_cents"] == 500 and rows[0]["description"] == "Coffee"


# ── import_expenses ───────────────────────────────────────────────────────────


def test_import_skips_zero_dedups_existing_allows_within_batch(conn):
    t = _tenant(conn)
    create_expense(conn, tenant_id=t["id"], amount_cents=5000, description="Lens",
                   incurred_on="2026-01-02", category="gear")
    rows = [
        {"amount_cents": 5000, "description": "Lens", "incurred_on": "2026-01-02", "category": "gear"},
        {"amount_cents": 0, "description": "Zero", "incurred_on": "2026-01-03"},
        {"amount_cents": 1200, "description": "Coffee", "incurred_on": "2026-01-03", "category": "other"},
        {"amount_cents": 1200, "description": "Coffee", "incurred_on": "2026-01-03", "category": "other"},
    ]
    s = import_expenses(conn, tenant_id=t["id"], rows=rows)
    assert s == {"imported": 2, "skipped_duplicate": 1, "skipped_zero": 1}   # both Coffees import
    # re-importing the same rows adds nothing (now all match existing)
    s2 = import_expenses(conn, tenant_id=t["id"], rows=rows)
    assert s2["imported"] == 0 and s2["skipped_duplicate"] == 3 and s2["skipped_zero"] == 1


def test_import_normalizes_unknown_category(conn):
    t = _tenant(conn)
    import_expenses(conn, tenant_id=t["id"],
                    rows=[{"amount_cents": 999, "description": "X", "category": "bogus"}])
    assert list_expenses(conn, t["id"])[0]["category"] == "other"


def test_import_dedup_is_per_tenant(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    create_expense(conn, tenant_id=t1["id"], amount_cents=5000, description="Lens",
                   incurred_on="2026-01-02")
    s = import_expenses(conn, tenant_id=t2["id"],
                        rows=[{"amount_cents": 5000, "description": "Lens", "incurred_on": "2026-01-02"}])
    assert s["imported"] == 1                                  # same row, different tenant → not a dup
    assert len(list_expenses(conn, t1["id"])) == 1 and len(list_expenses(conn, t2["id"])) == 1


# ── HTTP flow ─────────────────────────────────────────────────────────────────


def _tid(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def test_http_import_flow(client, app):
    creds = onboard_studio(client, email="exp@example.com")
    login_owner(client, creds)
    assert "Import expenses" in client.get("/finances/import").text

    data = b"date,description,amount\n2026-01-03,Coffee,5.00\n2026-01-04,Film,20.00\n"
    r = client.post("/finances/import", files={"file": ("exp.csv", data, "text/csv")})
    assert r.status_code == 200 and "imported" in r.text
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid(conn, creds["email"])
        descs = {e["description"] for e in list_expenses(conn, tid)}
        assert {"Coffee", "Film"} <= descs
        before = len(list_expenses(conn, tid))
    finally:
        conn.close()

    client.post("/finances/import", files={"file": ("exp.csv", data, "text/csv")})   # re-import
    conn = connect(app.state.settings.db_path)
    try:
        assert len(list_expenses(conn, _tid(conn, creds["email"]))) == before        # idempotent
    finally:
        conn.close()


def test_http_import_binary_friendly_error(client, app):
    creds = onboard_studio(client, email="expb@example.com")
    login_owner(client, creds)
    blob = bytes(range(256)) * 8
    r = client.post("/finances/import", files={"file": ("x.csv", blob, "application/octet-stream")})
    assert r.status_code == 200 and "look like a CSV" in r.text                       # not a 500
    conn = connect(app.state.settings.db_path)
    try:
        assert list_expenses(conn, _tid(conn, creds["email"])) == []
    finally:
        conn.close()
