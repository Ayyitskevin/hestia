"""End-to-end HTTP flow + auth gating via TestClient."""

from conftest import login_owner, onboard_studio


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "hestia"
    assert body["db"] == "ok"


def test_landing_public(client):
    assert client.get("/").status_code == 200


def test_api_requires_auth(client):
    assert client.get("/api/pipeline/runs").status_code == 401


def test_dashboard_redirects_when_anonymous(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_full_magic_moment(client):
    creds = onboard_studio(client, shoot_type="wedding")
    login_owner(client, creds)

    # create gallery
    r = client.post("/galleries", data={"title": "Sarah & Tom", "client_name": "Sarah"})
    gid = r.url.path.rstrip("/").split("/")[-1]

    # upload frames
    files = [("files", (f"img{i}.jpg", bytes([i]) * 64, "image/jpeg")) for i in range(5)]
    assert client.post(f"/galleries/{gid}/images", files=files).status_code == 200

    # process → vision + offer
    r = client.post(f"/galleries/{gid}/process")
    run_id = r.url.path.rstrip("/").split("/")[-1]
    status = client.get(f"/api/pipeline/runs/{run_id}").json()
    assert status["status"] == "done"
    assert status["offer_url"] and "/s/" in status["offer_url"]

    # the public client offer page renders with bundles
    offer_path = status["offer_url"].replace("http://testserver", "")
    page = client.get(offer_path)
    assert page.status_code == 200
    assert "bundle" in page.text.lower()


def test_double_process_one_offer_over_http(client):
    creds = onboard_studio(client, email="o2@example.com")
    login_owner(client, creds)
    gid = client.post("/galleries", data={"title": "G"}).url.path.rstrip("/").split("/")[-1]
    files = [("files", ("a.jpg", b"x" * 32, "image/jpeg"))]
    client.post(f"/galleries/{gid}/images", files=files)

    r1 = client.post(f"/galleries/{gid}/process")
    r2 = client.post(f"/galleries/{gid}/process")
    s1 = client.get(f"/api/pipeline/runs/{r1.url.path.split('/')[-1]}").json()
    s2 = client.get(f"/api/pipeline/runs/{r2.url.path.split('/')[-1]}").json()
    assert s1["offer_url"] == s2["offer_url"]


def test_tenant_isolation_on_runs(client):
    # studio A creates a run; studio B must not read it.
    a = onboard_studio(client, name="A", email="a@example.com")
    ca = login_owner(client.__class__(client.app), a)
    gid = ca.post("/galleries", data={"title": "GA"}).url.path.split("/")[-1]
    ca.post(f"/galleries/{gid}/images", files=[("files", ("a.jpg", b"x" * 16, "image/jpeg"))])
    run_id = ca.post(f"/galleries/{gid}/process").url.path.split("/")[-1]

    b = onboard_studio(client, name="B", email="b@example.com")
    cb = login_owner(client.__class__(client.app), b)
    assert cb.get(f"/api/pipeline/runs/{run_id}").status_code == 404
