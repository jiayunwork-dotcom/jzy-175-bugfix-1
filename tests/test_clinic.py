"""Hand-verifiable clinic benchmark: optimum exactly 3, proven optimal."""

from .conftest import CLINIC_CANDIDATES, CLINIC_RESIDENTS, submit_and_wait


def test_clinic_optimum_is_three_and_proven(client, clinic_version):
    _, vid = clinic_version
    job = submit_and_wait(client, vid, {"radius": 5})
    assert job["status"] == "completed"
    result = job["result"]
    assert result["feasible"] is True
    assert result["best_size"] == 3
    assert result["site_ids"] == ["c1", "c2", "c3"]
    # exhaustive search -> genuinely proven, never merely "greedy looks good"
    assert result["proven_optimal"] is True
    assert result["lower_bound"] == 3
    assert result["gap"] == 0


def test_clinic_each_cluster_site_is_essential(client, clinic_version):
    """Removing a site that every optimal solution needs -> infeasible,
    and the uncovered residents are listed (never a false success)."""
    pid, vid = clinic_version
    # version without c2: middle-cluster residents cannot be reached at all
    r = client.post(
        f"/api/projects/{pid}/versions",
        json={
            "residents": [dict(p) for p in CLINIC_RESIDENTS],
            "candidates": [dict(c) for c in CLINIC_CANDIDATES if c["id"] != "c2"],
        },
    )
    assert r.status_code == 201
    no_c2 = r.json()["id"]
    job = submit_and_wait(client, no_c2, {"radius": 5})
    result = job["result"]
    assert result["feasible"] is False
    assert set(result["uncovered_resident_ids"]) == {"r5", "r6", "r7", "r8"}
    assert job["status"] == "completed"


def test_force_nonoptimal_site_cannot_reduce_count(client, clinic_version):
    _, vid = clinic_version
    # forcing a decoy: optimum count cannot be below the original optimum (3),
    # and the forced decoy must actually appear in the solution
    job = submit_and_wait(client, vid, {"radius": 5, "forced_site_ids": ["d1"]})
    result = job["result"]
    assert result["feasible"] is True
    assert result["best_size"] >= 3
    assert "d1" in result["site_ids"]


def test_larger_radius_never_needs_more_sites(client, clinic_version):
    _, vid = clinic_version
    counts = {}
    for radius in (5, 10, 50):
        job = submit_and_wait(client, vid, {"radius": radius})
        counts[radius] = job["result"]["best_size"]
    assert counts[10] <= counts[5]
    assert counts[50] <= counts[10]
    assert counts[50] == 1  # one central site now reaches everyone
