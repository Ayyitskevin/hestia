"""Perceptual near-duplicate detection — burst-frame culling via aHash + Hamming."""

import io

import pytest

from hestia.galleries import add_image, create_gallery
from hestia.tenants import create_tenant
from hestia.vision import (
    DUP_HAMMING_THRESHOLD,
    _hamming_distance,
    _phash_from_hex,
    _phash_hex,
    cluster_duplicate_ids,
    perceptual_hash,
)

# ── Unit: the Hamming clustering logic (no Pillow needed) ────────────────────


def test_hamming_distance_counts_differing_bits():
    assert _hamming_distance(0, 0) == 0
    assert _hamming_distance(0, 1) == 1
    assert _hamming_distance(0b1111, 0b0000) == 4
    assert _hamming_distance(0xDEADBEEF, 0xDEADBEEF) == 0


def test_cluster_groups_near_phashes_keeps_best_keeper():
    # ids 1 & 2 differ by 2 bits (near-dup burst); id 3 is far.
    a = 0b1111_0000_1111_0000
    b = a ^ 0b0011_0000_0000_0000  # flip 2 bits
    c = 0xFFFFFFFFFFFFFFFF
    rows = [
        (1, _phash_hex(a), 0.9),
        (2, _phash_hex(b), 0.6),
        (3, _phash_hex(c), 0.3),
    ]
    dup = cluster_duplicate_ids(rows)
    # 1 has the higher keeper, so 2 is culled; 3 is not a near-dup of either.
    assert dup == {2}


def test_cluster_far_phashes_do_not_group():
    a = 0x0000000000000000
    b = 0xFFFFFFFFFFFFFFFF  # 64 bits apart — well over threshold
    rows = [(1, _phash_hex(a), 0.5), (2, _phash_hex(b), 0.5)]
    assert cluster_duplicate_ids(rows) == set()


def test_cluster_threshold_boundary():
    a = 0
    b = (1 << DUP_HAMMING_THRESHOLD) - 1  # exactly threshold bits apart
    rows = [(1, _phash_hex(a), 0.4), (2, _phash_hex(b), 0.5)]
    assert cluster_duplicate_ids(rows) == {1}  # boundary is inclusive


def test_cluster_exact_content_keys_fallback():
    # "d_" keys (Pillow-absent floor) cluster by exact equality, not Hamming.
    rows = [(1, "d_abc", 0.7), (2, "d_abc", 0.5), (3, "d_xyz", 0.9)]
    assert cluster_duplicate_ids(rows) == {2}


def test_cluster_back_compat_with_old_content_keys():
    # Analyses written before perceptual hashing carry "d_" keys and must still
    # cluster (exact-equality floor), not be misread as perceptual.
    rows = [(1, "d_deadbeef", 0.8), (2, "d_deadbeef", 0.6)]
    assert cluster_duplicate_ids(rows) == {2}


def test_phash_round_trip():
    h = 0x0123456789ABCDEF
    assert _phash_from_hex(_phash_hex(h)) == h
    assert _phash_from_hex("d_something") is None
    assert _phash_from_hex("") is None
    assert _phash_from_hex("p_not_hex") is None


# ── perceptual_hash (needs Pillow) ───────────────────────────────────────────


def test_perceptual_hash_none_for_non_image():
    pytest.importorskip("PIL")
    assert perceptual_hash(b"not an image at all") is None


def test_perceptual_hash_decodes_real_image():
    pytest.importorskip("PIL")
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (128, 128, 128)).save(buf, "JPEG")
    h = perceptual_hash(buf.getvalue())
    assert isinstance(h, int)


# ── End-to-end: analyze_gallery culls a burst via perceptual hashing ─────────


def _img_bytes(color, square_color=None, size=64) -> bytes:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (size, size), color)
    if square_color is not None:
        ImageDraw.Draw(im).rectangle(
            [size // 2 - 12, size // 2 - 12, size // 2 + 12, size // 2 + 12],
            fill=square_color,
        )
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def test_analyze_gallery_culls_near_duplicate_burst(conn, storage, settings):
    pytest.importorskip("PIL")
    from hestia.vision import MockVisionProvider, analyze_gallery

    t = create_tenant(conn, name="Burst Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Burst")
    # Two near-identical frames (uniform gray, one shifted by +2 — same aHash
    # pattern) plus one compositionally distinct frame (solid black).
    a = add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                  filename="burst_a.jpg", fileobj=io.BytesIO(_img_bytes((128, 128, 128), (255, 255, 255))),
                  content_type="image/jpeg")
    b = add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                  filename="burst_b.jpg", fileobj=io.BytesIO(_img_bytes((130, 130, 130), (255, 255, 255))),
                  content_type="image/jpeg")
    d = add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                  filename="distinct.jpg", fileobj=io.BytesIO(_img_bytes((0, 0, 0))),
                  content_type="image/jpeg")
    conn.commit()

    summary = analyze_gallery(
        conn, storage, settings, tenant_id=t["id"], gallery_id=g["id"],
        provider=MockVisionProvider(),
    )
    culled = set(summary["culled_image_ids"])
    # Exactly one of the burst pair is culled; the distinct frame is kept.
    assert summary["duplicate_count"] == 1
    assert d["id"] not in culled
    assert (a["id"] in culled) ^ (b["id"] in culled)
    # The persisted dup keys are perceptual ("p_…"), not content ("d_…").
    keys = [r["dup_key"] for r in conn.execute(
        "SELECT dup_key FROM image_analyses WHERE gallery_id = ?", (g["id"],))]
    assert all(k.startswith("p_") for k in keys)


def test_cull_summary_matches_run_cull(conn, storage, settings):
    """The owner-view recompute uses the same clustering as the live run."""
    pytest.importorskip("PIL")
    from hestia.vision import MockVisionProvider, analyze_gallery, cull_summary

    t = create_tenant(conn, name="Consistency Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="C")
    for i, color in enumerate([(128, 128, 128), (130, 130, 130), (0, 0, 0)]):
        add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"],
                  filename=f"f{i}.jpg", fileobj=io.BytesIO(_img_bytes(color)),
                  content_type="image/jpeg")
    conn.commit()
    summary = analyze_gallery(
        conn, storage, settings, tenant_id=t["id"], gallery_id=g["id"],
        provider=MockVisionProvider(),
    )
    recap = cull_summary(conn, t["id"], g["id"])
    assert recap["duplicate_ids"] == set(summary["culled_image_ids"]) - set()  # dup ids
    # culled_ids (dups ∪ blinks) should at least contain the run's dups
    assert set(summary["culled_image_ids"]).issuperset(recap["duplicate_ids"])
