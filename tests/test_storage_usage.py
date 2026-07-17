"""Truthful storage-metering visibility without quota or billing enforcement."""

from __future__ import annotations

import json

from conftest import ADMIN_TOKEN, CSRFClient, login_owner, onboard_studio

from hestia.auth import ADMIN
from hestia.galleries import create_gallery
from hestia.storage_usage import (
    format_storage_bytes,
    operator_storage_summary,
    tenant_storage_usage,
)
from hestia.tenants import create_tenant, create_user


def _project(conn, tenant_id: str, name: str = "Project") -> int:
    return conn.execute(
        "INSERT INTO projects (tenant_id, name) VALUES (?, ?)",
        (tenant_id, name),
    ).lastrowid


def _image(
    conn,
    *,
    tenant_id: str,
    gallery_id: int,
    filename: str,
    byte_count,
    storage_key: str,
    thumb_key: str | None = None,
    position: int = 0,
    hidden: int = 0,
) -> int:
    return conn.execute(
        "INSERT INTO images "
        "(tenant_id, gallery_id, filename, storage_key, content_type, bytes, position, "
        "thumb_key, hidden) VALUES (?, ?, ?, ?, 'image/jpeg', ?, ?, ?, ?)",
        (
            tenant_id,
            gallery_id,
            filename,
            storage_key,
            byte_count,
            position,
            thumb_key,
            hidden,
        ),
    ).lastrowid


def _project_file(
    conn,
    *,
    tenant_id: str,
    project_id: int,
    filename: str,
    byte_count,
    storage_key: str,
) -> int:
    return conn.execute(
        "INSERT INTO project_files "
        "(tenant_id, project_id, filename, storage_key, content_type, bytes) "
        "VALUES (?, ?, ?, ?, 'application/pdf', ?)",
        (tenant_id, project_id, filename, storage_key, byte_count),
    ).lastrowid


def _seed_usage(conn, first: dict, second: dict) -> None:
    first_gallery = create_gallery(conn, tenant_id=first["id"], title="First Gallery")
    second_gallery = create_gallery(conn, tenant_id=second["id"], title="Second Gallery")
    _image(
        conn,
        tenant_id=first["id"],
        gallery_id=first_gallery["id"],
        filename="tracked.jpg",
        byte_count=1024,
        storage_key="first/original.jpg",
        thumb_key="first/thumb.jpg",
        hidden=1,
    )
    _image(
        conn,
        tenant_id=first["id"],
        gallery_id=first_gallery["id"],
        filename="unknown-size.jpg",
        byte_count=None,
        storage_key="first/unknown.jpg",
        position=1,
    )
    _image(
        conn,
        tenant_id=first["id"],
        gallery_id=first_gallery["id"],
        filename="missing-object.jpg",
        byte_count=512,
        storage_key="   ",
        position=2,
    )
    for position, byte_count in enumerate(
        ("broken", 1.5, 2**63 - 1),
        start=3,
    ):
        _image(
            conn,
            tenant_id=first["id"],
            gallery_id=first_gallery["id"],
            filename=f"corrupt-{position}.jpg",
            byte_count=byte_count,
            storage_key=f"first/corrupt-{position}.jpg",
            position=position,
        )
    # Individually valid foreign keys but an inconsistent ownership relationship.
    # Matched joins must exclude it from both tenants rather than leak/misattribute it.
    _image(
        conn,
        tenant_id=second["id"],
        gallery_id=first_gallery["id"],
        filename="mismatched.jpg",
        byte_count=999_999,
        storage_key="wrong/mismatch.jpg",
        position=6,
    )
    _image(
        conn,
        tenant_id=second["id"],
        gallery_id=second_gallery["id"],
        filename="second.jpg",
        byte_count=8192,
        storage_key="second/original.jpg",
    )

    first_project = _project(conn, first["id"], "First Project")
    _project_file(
        conn,
        tenant_id=first["id"],
        project_id=first_project,
        filename="brief.pdf",
        byte_count=2048,
        storage_key="first/brief.pdf",
    )
    _project_file(
        conn,
        tenant_id=first["id"],
        project_id=first_project,
        filename="corrupt.pdf",
        byte_count=-1,
        storage_key="first/corrupt.pdf",
    )
    _project_file(
        conn,
        tenant_id=first["id"],
        project_id=first_project,
        filename="blob.pdf",
        byte_count=b"broken",
        storage_key="first/blob.pdf",
    )
    _project_file(
        conn,
        tenant_id=second["id"],
        project_id=first_project,
        filename="mismatched.pdf",
        byte_count=888_888,
        storage_key="wrong/mismatch.pdf",
    )
    conn.execute(
        "INSERT INTO product_sets (tenant_id, gallery_id, backend, variants_json) "
        "VALUES (?, ?, 'xai', ?)",
        (
            first["id"],
            first_gallery["id"],
            json.dumps(
                [
                    {
                        "status": "rendered",
                        "output_ref": "first/rendered-product.jpg",
                    }
                ]
            ),
        ),
    )
    conn.commit()


def test_format_storage_bytes_is_binary_and_fails_closed():
    assert format_storage_bytes(0) == "0 B"
    assert format_storage_bytes(1023) == "1023 B"
    assert format_storage_bytes(1024) == "1.0 KiB"
    assert format_storage_bytes(1536) == "1.5 KiB"
    assert format_storage_bytes(1024**3) == "1.0 GiB"
    assert format_storage_bytes(-1) == "0 B"
    assert format_storage_bytes(True) == "0 B"
    assert format_storage_bytes(None) == "0 B"


def test_tenant_usage_is_exact_scoped_and_marks_unknown_metadata(conn):
    first = create_tenant(conn, name="First Studio", shoot_type="wedding")
    second = create_tenant(conn, name="Second Studio", shoot_type="portrait")
    _seed_usage(conn, first, second)

    usage = tenant_storage_usage(conn, first["id"])

    assert usage["tracked_bytes"] == 3072
    assert usage["tracked_display"] == "3.0 KiB"
    assert usage["gallery_originals"] == {
        "objects": 6,
        "tracked_objects": 1,
        "unknown_objects": 5,
        "bytes": 1024,
        "display": "1.0 KiB",
    }
    assert usage["project_files"] == {
        "objects": 3,
        "tracked_objects": 1,
        "unknown_objects": 2,
        "bytes": 2048,
        "display": "2.0 KiB",
    }
    assert usage["unknown_object_rows"] == 7
    assert usage["unmetered_thumbnail_objects"] == 1
    assert usage["object_rows"] == 9
    assert usage["tracked_object_rows"] == 2
    assert tenant_storage_usage(conn, second["id"])["tracked_bytes"] == 8192


def test_equal_sized_rows_are_neither_multiplied_nor_deduplicated(conn):
    tenant = create_tenant(conn, name="Equal Studio", shoot_type="wedding")
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="Equal Gallery")
    project_id = _project(conn, tenant["id"])
    for position in range(2):
        _image(
            conn,
            tenant_id=tenant["id"],
            gallery_id=gallery["id"],
            filename=f"equal-{position}.jpg",
            byte_count=1024,
            storage_key=f"equal/{position}.jpg",
            position=position,
        )
    for position in range(3):
        _project_file(
            conn,
            tenant_id=tenant["id"],
            project_id=project_id,
            filename=f"equal-{position}.pdf",
            byte_count=1024,
            storage_key=f"equal/{position}.pdf",
        )
    conn.commit()

    usage = tenant_storage_usage(conn, tenant["id"])

    assert usage["gallery_originals"]["bytes"] == 2048
    assert usage["project_files"]["bytes"] == 3072
    assert usage["tracked_bytes"] == 5120


def test_operator_summary_is_two_queries_includes_zero_studios_and_sorts(conn):
    first = create_tenant(conn, name="First Studio", shoot_type="wedding")
    second = create_tenant(conn, name="Second Studio", shoot_type="portrait")
    zero = create_tenant(conn, name="Zero Studio", shoot_type="food")
    _seed_usage(conn, first, second)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    summary = operator_storage_summary(conn)

    conn.set_trace_callback(None)
    selects = [
        sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    assert len(selects) == 2
    assert summary["tracked_bytes"] == 11_264
    assert summary["tracked_display"] == "11.0 KiB"
    assert summary["unknown_object_rows"] == 7
    assert summary["unmetered_thumbnail_objects"] == 1
    assert [row["tenant_id"] for row in summary["tenants"]] == [
        second["id"],
        first["id"],
        zero["id"],
    ]
    assert summary["tenants"][-1]["tracked_bytes"] == 0
    assert tenant_storage_usage(conn, zero["id"])["gallery_originals"]["bytes"] == 0
    assert len(operator_storage_summary(conn, limit=2)["tenants"]) == 2


def test_owner_account_renders_only_its_tracked_metering_basis(client, conn):
    creds = onboard_studio(client, name="Metered Studio", email="metered@example.com")
    login_owner(client, creds)
    tenant = conn.execute(
        "SELECT * FROM tenants WHERE name = 'Metered Studio'"
    ).fetchone()
    other = create_tenant(conn, name="Foreign Studio", shoot_type="wedding")
    _seed_usage(conn, dict(tenant), other)

    page = client.get("/settings/account")
    text = " ".join(page.text.split())

    assert page.status_code == 200
    assert "Tracked storage" in text
    assert "3.0 KiB" in text
    assert "Gallery originals" in text and "Project files" in text
    assert "7 records have missing or untrusted size/storage-key metadata" in text
    assert "records with known bytes" in text
    assert "Relationship-inconsistent rows" in text
    assert "No storage quota or billing is enforced" in text
    assert "thumbnails, generated product renders" in text
    assert format_storage_bytes(999_999) not in text
    assert "$" not in text.split("Tracked storage", 1)[1]


def test_account_storage_visibility_remains_owner_only(client, conn):
    creds = onboard_studio(client, name="Owner Boundary", email="boundary@example.com")
    tenant = conn.execute(
        "SELECT * FROM tenants WHERE name = 'Owner Boundary'"
    ).fetchone()
    create_user(
        conn,
        tenant_id=tenant["id"],
        email="secondary@example.com",
        password="secondary-pass",
        role=ADMIN,
    )
    conn.commit()
    secondary = CSRFClient(client.app)
    secondary.post(
        "/login",
        data={"email": "secondary@example.com", "password": "secondary-pass"},
    )

    anonymous = CSRFClient(client.app).get(
        "/settings/account",
        follow_redirects=False,
    )
    forbidden = secondary.get("/settings/account", follow_redirects=False)

    assert anonymous.status_code == 303 and anonymous.headers["location"] == "/login"
    assert forbidden.status_code == 303
    assert forbidden.headers["location"] == "/settings/site?forbidden=1"
    assert creds["email"] == "boundary@example.com"


def test_invalid_custom_domain_rerender_keeps_storage_context(client):
    creds = onboard_studio(client, email="domain-storage@example.com")
    login_owner(client, creds)

    page = client.post(
        "/settings/account/domain",
        data={"custom_domain": "not a domain"},
    )

    assert page.status_code == 400
    assert "Enter a valid domain" in page.text
    assert "Tracked storage footprint" in page.text
    assert "0 B" in page.text


def test_admin_system_renders_operator_rollup(app, conn):
    first = create_tenant(conn, name="First Studio", shoot_type="wedding")
    second = create_tenant(conn, name="Second Studio", shoot_type="portrait")
    _seed_usage(conn, first, second)
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})

    page = admin.get("/admin/system")

    assert page.status_code == 200
    assert "Tracked storage metadata" in page.text
    assert "11.0 KiB" in page.text
    assert "First Studio" in page.text and "Second Studio" in page.text
    assert "No quota, invoice, or physical-object-store total is derived here" in page.text
    assert "Relationship-inconsistent" in page.text
