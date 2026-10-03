"""Radius sweep: a background job producing a monotone station-count ladder."""

import math

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


def test_sweep_every_completed_step_closes_gap(client):
    """Every completed, proven-optimal sweep point must report
    lower_bound == best_size and gap == 0, including a step whose root
    packing bound is strictly below the optimum."""
    h = math.sqrt(3) / 2
    residents = [
        {"id": "a", "x": 0.0, "y": 0.0},
        {"id": "b", "x": 1.0, "y": 0.0},
        {"id": "c", "x": 0.5, "y": h},
    ]
    candidates = [
        {"id": "m_ab", "x": 0.5, "y": 0.0},
        {"id": "m_bc", "x": 0.75, "y": h / 2},
        {"id": "m_ca", "x": 0.25, "y": h / 2},
    ]
    r = client.post("/api/projects", json={"name": "sweep-gap"})
    pid = r.json()["id"]
    r = client.post(
        f"/api/projects/{pid}/versions",
        json={"residents": residents, "candidates": candidates},
    )
    vid = r.json()["id"]

    r = client.post(
        f"/api/versions/{vid}/sweep",
        json={"radii": [0.9, 0.6]},  # executed ascending
    )
    job = wait_for_job(client, r.json()["job_id"])
    assert job["status"] == "completed"
    result = job["result"]
    assert result["stop_reason"] == "completed"
    assert [p["radius"] for p in result["points"]] == [0.6, 0.9]
    counts = [p["best_size"] for p in result["points"]]
    assert counts == sorted(counts, reverse=True)
    # at 0.6 the optimum is 2 while the packing bound is only 1: this is the
    # step that used to come back proven_optimal with a positive gap
    assert result["points"][0]["best_size"] == 2
    for p in result["points"]:
        assert p["feasible"] is True
        assert p["proven_optimal"] is True
        assert p["lower_bound"] == p["best_size"]
        assert p["gap"] == 0
