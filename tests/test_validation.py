"""Field-anchored validation errors and infeasibility reporting."""

from .conftest import CLINIC_CANDIDATES, CLINIC_RESIDENTS, submit_and_wait


def _make_project(client):
    r = client.post("/api/projects", json={"name": "p"})
    return r.json()["id"]


def test_radius_non_positive_points_at_field(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/versions",
        json={"residents": CLINIC_RESIDENTS, "candidates": CLINIC_CANDIDATES},
    )
    vid = r.json()["id"]
    for bad_radius in (0, -3):
        r = client.post(f"/api/versions/{vid}/solve", json={"radius": bad_radius})
        assert r.status_code == 400
        fields = [e["field"] for e in r.json()["error"]["errors"]]
        assert "radius" in fields


def test_empty_residents_and_candidates(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/versions",
        json={"residents": [], "candidates": CLINIC_CANDIDATES},
    )
    assert r.status_code == 400
    assert "residents" in [e["field"] for e in r.json()["error"]["errors"]]

    r = client.post(
        f"/api/projects/{pid}/versions",
        json={"residents": CLINIC_RESIDENTS, "candidates": []},
    )
    assert r.status_code == 400
    assert "candidates" in [e["field"] for e in r.json()["error"]["errors"]]


def test_unknown_forced_site_id(client, clinic_version):
    _, vid = clinic_version
    r = client.post(
        f"/api/versions/{vid}/solve",
        json={"radius": 5, "forced_site_ids": ["nope"]},
    )
    assert r.status_code == 400
    err = r.json()["error"]["errors"][0]
    assert err["field"] == "forced_site_ids"
    assert "nope" in err["message"]


def test_all_open_still_uncoverable_lists_points_and_no_success(client):
    pid = _make_project(client)
    residents = [{"id": "a", "x": 0, "y": 0}, {"id": "far", "x": 999, "y": 999}]
    r = client.post(
        f"/api/projects/{pid}/versions",
        json={"residents": residents, "candidates": [{"id": "s", "x": 0, "y": 0}]},
    )
    vid = r.json()["id"]
    job = submit_and_wait(client, vid, {"radius": 1})
    result = job["result"]
    assert result["feasible"] is False
    assert result["uncovered_resident_ids"] == ["far"]
    assert result["proven_optimal"] is True  # infeasibility is itself proven
    assert job["status"] == "completed"


def test_bad_coordinates_field_paths(client):
    pid = _make_project(client)
    r = client.post(
        f"/api/projects/{pid}/versions",
        json={
            "residents": [{"id": "r1", "x": "oops", "y": 0}],
            "candidates": CLINIC_CANDIDATES,
        },
    )
    assert r.status_code == 400
    assert "residents[0].x" in [e["field"] for e in r.json()["error"]["errors"]]
