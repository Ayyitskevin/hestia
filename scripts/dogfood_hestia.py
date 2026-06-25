#!/usr/bin/env python3
"""Dogfood driver — drive the real running Hestia through the magic moment.

Admin-onboards a studio, logs in as the owner, creates a gallery, uploads sample
frames, processes it (vision → offer), polls until done, and asserts a real,
clickable offer URL came out the other end. One app, no fleet.

Usage: python scripts/dogfood_hestia.py http://127.0.0.1:8590
Env:   HESTIA_API_TOKEN must match the running server's admin token.
"""

from __future__ import annotations

import os
import sys
import time
from urllib.parse import urlparse

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8590"
ADMIN_TOKEN = os.environ.get("HESTIA_API_TOKEN", "dogfood-admin")
EMAIL, PASSWORD = "dogfood@studio.test", "dogfood-pw"


def main() -> int:
    t0 = time.time()
    # Admin onboards a studio.
    admin = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    r = admin.post("/admin/onboarding", data={
        "name": "Dogfood Studio", "shoot_type": "wedding",
        "owner_email": EMAIL, "owner_password": PASSWORD,
    })
    assert "hestia_tk_" in r.text, "onboarding did not mint an API key"
    print("✓ studio onboarded")

    # Owner logs in and creates a gallery.
    owner = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
    owner.post("/login", data={"email": EMAIL, "password": PASSWORD})
    r = owner.post("/galleries", data={"title": "Dogfood Wedding", "client_name": "Pat & Sam"})
    gid = str(r.url).rstrip("/").split("/")[-1]
    print(f"✓ gallery {gid} created")

    # Upload sample frames (bytes are fine — mock vision keys on filename).
    files = [("files", (f"frame-{i:02d}.jpg", bytes([i % 256]) * 256, "image/jpeg"))
             for i in range(8)]
    owner.post(f"/galleries/{gid}/images", files=files)
    print("✓ 8 frames uploaded")

    # Process → vision → offer.
    proc_t = time.time()
    r = owner.post(f"/galleries/{gid}/process")
    run_id = str(r.url).rstrip("/").split("/")[-1]

    offer_url, status = None, None
    for _ in range(60):
        data = owner.get(f"/api/pipeline/runs/{run_id}").json()
        status = data["status"]
        if status in ("done", "error"):
            offer_url = data.get("offer_url")
            break
        time.sleep(0.5)
    elapsed = time.time() - proc_t

    if status != "done" or not offer_url:
        print(f"✗ pipeline ended status={status} offer_url={offer_url}", file=sys.stderr)
        return 1

    # The offer URL must actually render a client storefront.
    page = owner.get(urlparse(offer_url).path)
    assert page.status_code == 200 and "bundle" in page.text.lower(), "offer page did not render"

    # Idempotency: re-process, assert the same link.
    r2 = owner.post(f"/galleries/{gid}/process")
    run2 = str(r2.url).rstrip("/").split("/")[-1]
    for _ in range(60):
        d2 = owner.get(f"/api/pipeline/runs/{run2}").json()
        if d2["status"] in ("done", "error"):
            break
        time.sleep(0.5)
    assert d2.get("offer_url") == offer_url, "re-process produced a DIFFERENT offer URL!"

    print("\n🔥 MAGIC MOMENT")
    print(f"   offer URL : {offer_url}")
    print(f"   vision→offer in {elapsed:.1f}s · total {time.time() - t0:.1f}s")
    print("   idempotent: re-process produced the same link ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
