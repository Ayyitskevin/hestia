"""Demo tour → live studio links — once the founder demos are seeded, every tour
page grows a CTA into the real, processed demo studio (the tour describes the
moat; the link proves it). Unseeded environments show no dead link."""

from hestia.founder_demo import (
    FOUNDER_DEMO_STUDIOS,
    live_demo_studio_url,
    seed_founder_demo_studios,
)


def test_unseeded_tour_has_no_dead_link(client, conn):
    assert live_demo_studio_url(conn, "/demo/wedding") == ""
    page = client.get("/demo/wedding").text
    assert "Tour a live demo studio" not in page


def test_seeded_tours_link_their_live_studio(client, conn, storage, settings):
    seed_founder_demo_studios(conn, settings, storage)
    conn.commit()
    for spec in FOUNDER_DEMO_STUDIOS:
        expected = f"/studio/{spec['slug']}"
        assert live_demo_studio_url(conn, spec["landing_path"]) == expected
        page = client.get(spec["landing_path"]).text
        assert "Tour a live demo studio" in page
        assert f'href="{expected}"' in page
        # and the CTA target is actually live, not a 404
        assert client.get(expected).status_code == 200


def test_unknown_landing_path_maps_nowhere(conn):
    assert live_demo_studio_url(conn, "/demo/underwater-basket") == ""
