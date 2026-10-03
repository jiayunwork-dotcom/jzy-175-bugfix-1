"""Result self-consistency: proven-optimal <=> lower_bound == best_size, gap 0.

Covers the three acceptance areas for the search-status fields:

* a completed, proven-optimal solve reports ``lower_bound == best_size`` and
  ``gap == 0`` (engine level and over HTTP), while a stopped-early solve keeps
  a valid loose bound and is never flagged optimal;
* every point of a completed radius sweep satisfies the same relation;
* records written by the pre-fix solver (``proven_optimal=true`` with a loose
  stored bound) are repaired once at startup and read back self-consistently
  on all three legacy read paths -- dedup reuse, per-version history, and the
  incremental parent-seed lookup -- without changing the chosen sites or the
  station counts.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app import main as mainmod
from app.main import _radius_dedup
from app.solver import exact_set_cover

from .conftest import (
    CLINIC_CANDIDATES,
    CLINIC_RESIDENTS,
    submit_and_wait,
)

# ---------------------------------------------------------------------------
# A geometrically realisable instance whose *root* packing bound is strictly
# below the optimum (the shape that exposed the bug):
#
#   r1=(0,0) r2=(4,0) r3=(2,3)
#   c1=(2,0) c2=(3,1.5) c3=(1,1.5), radius 2
#
#   c1 covers {r1,r2}   c2 covers {r2,r3}   c3 covers {r1,r3}
#
# Every pair of residents shares a candidate, so the disjoint-packing bound is
# 1, but no single site covers everyone -> optimum is exactly 2.  At radius
# 3.1, c1 reaches all three -> optimum 1.
# ---------------------------------------------------------------------------

TRIANGLE_RESIDENTS = [
    {"id": "r1", "x": 0, "y": 0},
    {"id": "r2", "x": 4, "y": 0},
    {"id": "r3", "x": 2, "y": 3},
]
TRIANGLE_CANDIDATES = [
    {"id": "c1", "x": 2, "y": 0},
    {"id": "c2", "x": 3, "y": 1.5},
    {"id": "c3", "x": 1, "y": 1.5},
]
TRIANGLE_RELATION = {
    "r1": ["c1", "c3"],
    "r2": ["c1", "c2"],
    "r3": ["c2", "c3"],
}


def _make_version(client, residents, candidates, name="p"):
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = client.post(
        f"/api/projects/{pid}/versions",
        json={"residents": residents, "candidates": candidates},
    )
    assert r.status_code == 201, r.text
    return pid, r.json()["id"]


# ---------------------------------------------------------------- engine ----
def test_engine_proven_optimal_raises_bound_to_incumbent():
    """Root packing bound (1) < optimum (2): completion must report lb == best."""
    res = exact_set_cover(
        ["r1", "r2", "r3"], TRIANGLE_RELATION, ["c1", "c2", "c3"]
    )
    assert res.stop_reason == "completed"
    assert res.proven_optimal is True
    assert res.best_size == 2
    assert res.lower_bound == 2
    assert res.gap == 0


# ------------------------------------------------------------------- HTTP ----
def test_http_completed_solve_has_tight_bound(client):
    _, vid = _make_version(client, TRIANGLE_RESIDENTS, TRIANGLE_CANDIDATES)
    job = submit_and_wait(client, vid, {"radius": 2.0})
    assert job["status"] == "completed"
    res = job["result"]
    assert res["proven_optimal"] is True
    assert res["best_size"] == 2
    assert res["lower_bound"] == res["best_size"]
    assert res["gap"] == 0
    # and the reported plan really covers everyone
    assert set(res["site_ids"]) <= {"c1", "c2", "c3"}
    assert len(res["site_ids"]) == 2


def test_sweep_every_point_has_tight_bound(client):
    _, vid = _make_version(client, TRIANGLE_RESIDENTS, TRIANGLE_CANDIDATES)
    job = submit_and_wait(client, vid, {"radii": [3.1, 2.0]}, path="sweep")
    assert job["status"] == "completed"
    points = job["result"]["points"]
    assert [p["radius"] for p in points] == [2.0, 3.1]
    assert [p["best_size"] for p in points] == [2, 1]  # monotone ladder
    for p in points:
        assert p["feasible"] is True
        assert p["proven_optimal"] is True
        assert p["lower_bound"] == p["best_size"]
        assert p["gap"] == 0


# ------------------------------------------------------------------ legacy ----

# Result shapes as the pre-fix solver persisted them: proven_optimal=true with
# the loose root bound and a non-zero gap.
LEGACY_SOLVE_RESULT = {
    "feasible": True,
    "proven_optimal": True,
    "stop_reason": "completed",
    "lower_bound": 1,
    "nodes_explored": 12,
    "max_depth": 2,
    "gap": 2,
    "site_ids": ["c1", "c2", "c3"],
    "best_size": 3,
}

LEGACY_SWEEP_RESULT = {
    "feasible": True,
    "points": [
        {
            "radius": 5.0,
            "feasible": True,
            "proven_optimal": True,
            "stop_reason": "completed",
            "lower_bound": 1,
            "nodes_explored": 9,
            "max_depth": 2,
            "gap": 2,
            "site_ids": ["c1", "c2", "c3"],
            "best_size": 3,
        },
        {
            "radius": 50.0,
            "feasible": True,
            "proven_optimal": True,
            "stop_reason": "completed",
            "lower_bound": 1,
            "nodes_explored": 3,
            "max_depth": 1,
            "gap": 0,
            "site_ids": ["c3"],
            "best_size": 1,
        },
    ],
    "completed_steps": 2,
    "total_steps": 2,
    "stop_reason": "completed",
}

# A timeout record is NOT contradictory: its loose bound stays valid, so the
# startup repair must leave it byte-for-byte alone.
LEGACY_TIMEOUT_RESULT = {
    "feasible": True,
    "proven_optimal": False,
    "stop_reason": "timeout",
    "lower_bound": 3,
    "nodes_explored": 100,
    "max_depth": 5,
    "gap": 1,
    "site_ids": ["c1", "c2", "c3", "d1"],
    "best_size": 4,
}


def _plant_legacy_records(client, pid, vid):
    """Write pre-upgrade-style job rows into the database behind ``client``."""
    store = client.app.state.store

    solve_id = uuid.uuid4().hex
    store.insert_job(
        solve_id,
        pid,
        vid,
        "solve",
        {"radius": 5.0, "forced_site_ids": []},
        dedup_key=_radius_dedup(vid, {"radius": 5.0, "forced_site_ids": []}, "solve"),
    )
    store.update_job_progress(
        solve_id,
        {
            "nodes_explored": 12,
            "search_depth": 2,
            "lower_bound": 1,
            "best_size": 3,
            "best_site_ids": ["c1", "c2", "c3"],
        },
    )
    store.finish_job(solve_id, "completed", LEGACY_SOLVE_RESULT)

    sweep_id = uuid.uuid4().hex
    store.insert_job(
        sweep_id,
        pid,
        vid,
        "sweep",
        {"radii": [5.0, 50.0], "forced_site_ids": []},
        dedup_key=None,
    )
    store.finish_job(sweep_id, "completed", LEGACY_SWEEP_RESULT)

    timeout_id = uuid.uuid4().hex
    store.insert_job(
        timeout_id,
        pid,
        vid,
        "solve",
        {"radius": 5.0, "forced_site_ids": ["d1"]},
        dedup_key=_radius_dedup(
            vid, {"radius": 5.0, "forced_site_ids": ["d1"]}, "solve"
        ),
    )
    store.finish_job(timeout_id, "timeout", LEGACY_TIMEOUT_RESULT)

    return solve_id, sweep_id, timeout_id


def test_legacy_records_repaired_on_startup_all_read_paths(
    client, tmp_path, monkeypatch
):
    pid, vid = _make_version(client, CLINIC_RESIDENTS, CLINIC_CANDIDATES, name="legacy")
    solve_id, sweep_id, timeout_id = _plant_legacy_records(client, pid, vid)

    # --- upgrade: a fresh process opens the same database -----------------
    monkeypatch.setattr(mainmod, "WORKERS", 2)
    with TestClient(mainmod.app) as c2:
        assert c2.app.state.manager.legacy_results_repaired == 2

        # path 1: dedup reuse returns the (now coherent) old record, and the
        # plan itself -- sites and count -- is exactly what it was
        r = c2.post(f"/api/versions/{vid}/solve", json={"radius": 5.0})
        assert r.json()["submit_outcome"] == "reused"
        assert r.json()["job_id"] == solve_id
        j = c2.get(f"/api/jobs/{solve_id}").json()
        res = j["result"]
        assert res["site_ids"] == ["c1", "c2", "c3"]
        assert res["best_size"] == 3
        assert res["proven_optimal"] is True
        assert res["lower_bound"] == 3
        assert res["gap"] == 0
        assert j["progress"]["lower_bound"] == 3  # final snapshot aligned too

        # path 2: per-version history reads back self-consistently
        rows = {row["id"]: row for row in c2.get(f"/api/versions/{vid}/jobs").json()}
        for row in rows.values():
            result = row["result"]
            if result and result.get("proven_optimal") and result.get("feasible"):
                if "points" in result:
                    for p in result["points"]:
                        assert p["lower_bound"] == p["best_size"]
                        assert p["gap"] == 0
                else:
                    assert result["lower_bound"] == result["best_size"]
                    assert result["gap"] == 0

        # the sweep record: loose point repaired, already-tight point and all
        # site choices untouched
        sweep = rows[sweep_id]["result"]
        p5, p50 = sweep["points"]
        assert (p5["lower_bound"], p5["best_size"], p5["gap"]) == (3, 3, 0)
        assert p5["site_ids"] == ["c1", "c2", "c3"]
        assert (p50["lower_bound"], p50["best_size"], p50["gap"]) == (1, 1, 0)
        assert p50["site_ids"] == ["c3"]

        # the timeout record was valid as stored -> untouched
        assert rows[timeout_id]["result"] == LEGACY_TIMEOUT_RESULT

        # path 3: incremental re-solve on a derived version still picks the
        # repaired record as its warm-start seed
        r = c2.post(
            f"/api/versions/{vid}/derive",
            json={"add_residents": [{"id": "r12", "x": 20.5, "y": 35.5}]},
        )
        assert r.status_code == 201, r.text
        vid2 = r.json()["id"]
        inc = submit_and_wait(c2, vid2, {"radius": 5.0}, path="solve-incremental")
        assert inc["result"]["incremental"]["warm_started_from_parent"] is True
        assert inc["result"]["incremental"]["parent_version_id"] == vid
        # and the fresh result itself honours the tightened contract
        assert inc["result"]["proven_optimal"] is True
        assert inc["result"]["best_size"] == 3
        assert inc["result"]["lower_bound"] == 3
        assert inc["result"]["gap"] == 0

    # --- the repair is idempotent: a second restart changes nothing -------
    with TestClient(mainmod.app) as c3:
        assert c3.app.state.manager.legacy_results_repaired == 0
        j = c3.get(f"/api/jobs/{solve_id}").json()
        assert j["result"]["lower_bound"] == 3
        assert j["result"]["gap"] == 0
        assert j["result"]["site_ids"] == ["c1", "c2", "c3"]
