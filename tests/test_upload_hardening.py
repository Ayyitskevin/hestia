"""Slice 1 input-validation hardening: uploads can't OOM the shared container, and
public free-text can't bloat rows/emails. Both are multi-tenant availability defenses
— one studio's oversized input must never degrade the box for the others."""

import io

from hestia import galleries
from hestia.crm import get_client
from hestia.galleries import add_image, create_gallery, list_images
from hestia.studio import create_inquiry
from hestia.tenants import create_tenant


def test_add_image_rejects_empty_and_oversize(conn, storage, monkeypatch):
    monkeypatch.setattr(galleries, "_MAX_IMAGE_BYTES", 16)   # tiny cap for a fast test
    t = create_tenant(conn, name="Cap Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Finals")

    ok = add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                   filename="ok.jpg", fileobj=io.BytesIO(b"x" * 16), content_type="image/jpeg")
    assert ok is not None                                    # exactly at the ceiling is fine

    big = add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                    filename="big.jpg", fileobj=io.BytesIO(b"x" * 17), content_type="image/jpeg")
    assert big is None                                       # one byte over → rejected, not stored

    empty = add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                      filename="empty.jpg", fileobj=io.BytesIO(b""), content_type="image/jpeg")
    assert empty is None                                     # empty → rejected

    assert len(list_images(conn, g["id"])) == 1              # only the valid frame landed


def test_bounded_read_never_pulls_the_whole_blob(conn, storage, monkeypatch):
    """The defense is a *bounded read* — never materialize more than the ceiling+1 in
    memory, even if the client streams gigabytes. Prove read() is called with a limit."""
    monkeypatch.setattr(galleries, "_MAX_IMAGE_BYTES", 100)
    t = create_tenant(conn, name="Stream Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Finals")

    reads: list[int | None] = []

    class SpyFile(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
              filename="huge.jpg", fileobj=SpyFile(b"x" * 5000), content_type="image/jpeg")
    assert reads and reads[0] == 101                         # read(_MAX + 1), never read(-1)


def test_public_inquiry_caps_free_text(conn):
    t = create_tenant(conn, name="Inquiry Studio", shoot_type="wedding")
    huge = "A" * 100_000
    project = create_inquiry(conn, tenant=t, name=huge, email=huge + "@x.test",
                             message=huge, event_date=huge, lead_source=huge)
    client = get_client(conn, t["id"], project["client_id"])

    assert len(client["name"]) <= 200
    assert len(client["email"]) <= 254
    assert len(project["notes"]) <= 20_000
    assert len(project["name"]) <= 300
    assert len(project["lead_source"]) <= 50                 # already capped; still holds
