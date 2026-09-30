"""Radius sweep: a background job producing a monotone station-count ladder."""

from .conftest import CLINIC_CANDIDATES, CLINIC_RESIDENTS, submit_and_wait, wait_for_job


def test_sweep_returns_monotone_ladder(client):
    r = client.post("/api/projects", json={"name": "sweep"})
    pid = r.json()["id"]
    r = client.post(
        f"/api/projects/{pid}/versions",
        json={"residents": CLINIC_RESIDENTS, "candidates": CLINIC_CANDIDATES},
    )
    vid = r.json()["id"]

    r = client.post(
        f"/api/versions/{vid}/sweep",
        json={"radii": [50, 5, 20, 10], "forced_site_ids": []},
    )
    assert r.status_code == 202
    job = wait_for_job(client, r.json()["job_id"])
    assert job["status"] == "completed"
    result = job["result"]
    assert result["stop_reason"] == "completed"
    # submitted out of order, executed ascending
    radii = [p["radius"] for p in result["points"]]
    assert radii == [5.0, 10.0, 20.0, 50.0]
    counts = [p["best_size"] for p in result["points"]]
    assert counts == sorted(counts, reverse=True)  # bigger radius -> no more sites
    assert counts[0] == 3 and counts[-1] == 1
    for p in result["points"]:
        assert p["proven_optimal"] is True
        # full coverage at every step
        assert p["feasible"] is True


def test_sweep_validation_nonpositive_radius(client):
    r = client.post("/api/projects", json={"name": "sweep2"})
    pid = r.json()["id"]
    r = client.post(
        f"/api/projects/{pid}/versions",
        json={"residents": CLINIC_RESIDENTS, "candidates": CLINIC_CANDIDATES},
    )
    vid = r.json()["id"]
    r = client.post(f"/api/versions/{vid}/sweep", json={"radii": [1, -2]})
    assert r.status_code == 400
    assert r.json()["error"]["errors"][0]["field"] == "radii"
