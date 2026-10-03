"""Pre-upgrade database compatibility.

Old builds persisted rows where a completed, proven-optimal result carried a
stale root packing lower bound (best_size=41/lower_bound=39 style). The fix
for already-stored rows is **read-time normalisation in the hydration layer**:
stored bytes are never rewritten, so every historical version keeps exactly
the plan it was solved with. These tests start the service against a database
written in the old shape and exercise the three read paths:

1. dedup reuse on resubmission,
2. per-version history retrieval,
3. incremental re-solve taking the parent solution as a warm start,

plus the guarantees that early-stopped/infeasible rows are left alone and
that the plan fields (site_ids / best_size / uncovered lists) never change.
"""

from __future__ import annotations

import json
import math
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main as mainmod
from app.store import Store

from .conftest import wait_for_job

H = math.sqrt(3) / 2

RESIDENTS = [
    {"id": "a", "x": 0.0, "y": 0.0},
    {"id": "b", "x": 1.0, "y": 0.0},
    {"id": "c", "x": 0.5, "y": H},
]
CANDIDATES = [
    {"id": "m_ab", "x": 0.5, "y": 0.0},
    {"id": "m_bc", "x": 0.75, "y": H / 2},
    {"id": "m_ca", "x": 0.25, "y": H / 2},
]


def _feasible_result(best_size, lower_bound, gap, site_ids, stop_reason="completed",
                     proven=True):
    return {
        "feasible": True,
        "proven_optimal": proven,
        "stop_reason": stop_reason,
        "lower_bound": lower_bound,
        "nodes_explored": 123,
        "max_depth": 4,
        "gap": gap,
        "site_ids": site_ids,
        "best_size": best_size,
    }


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A data directory left behind by the pre-fix build (old-shape rows)."""
    db_path = str(tmp_path / "siting.db")
    monkeypatch.setattr(mainmod, "DB_PATH", db_path)
    monkeypatch.setattr(mainmod, "WORKERS", 2)

    store = Store(db_path)
    store.create_project("p1", "legacy")
    store.create_version("v1", "p1", RESIDENTS, CANDIDATES, None, "")

    # 1) completed single solve with a self-contradictory search state:
    #    proven optimal, optimum 2, stale packing bound 1, gap 1.
    params_06 = {"radius": 0.6, "forced_site_ids": []}
    dedup = mainmod._radius_dedup("v1", params_06, "solve")
    store.insert_job("j1", "p1", "v1", "solve", params_06, dedup_key=dedup)
    store.finish_job(
        "j1",
        "completed",
        _feasible_result(2, 1, 1, ["m_ab", "m_bc"]),
    )

    # 2) completed sweep whose first step has the same contradiction.
    sweep_params = {"radii": [0.6, 0.9], "forced_site_ids": []}
    store.insert_job("j2", "p1", "v1", "sweep", sweep_params)
    store.finish_job(
        "j2",
        "completed",
        {
            "feasible": True,
            "completed_steps": 2,
            "total_steps": 2,
            "stop_reason": "completed",
            "points": [
                {"radius": 0.6, **_feasible_result(2, 1, 1, ["m_ab", "m_bc"])},
                {"radius": 0.9, **_feasible_result(1, 1, 0, ["m_ab"])},
            ],
        },
    )

    # 3) early-stopped row: bound is a valid relaxation bound and must stay.
    params_07 = {"radius": 0.7, "forced_site_ids": []}
    store.insert_job("j3", "p1", "v1", "solve", params_07,
                     dedup_key=mainmod._radius_dedup("v1", params_07, "solve"))
    store.finish_job(
        "j3",
        "timeout",
        _feasible_result(3, 1, 2, ["m_ab", "m_bc", "m_ca"],
                         stop_reason="timeout", proven=False),
    )

    # 4) completed infeasible row: uncovered list must come back untouched.
    params_055 = {"radius": 0.55, "forced_site_ids": []}
    store.insert_job("j4", "p1", "v1", "solve", params_055,
                     dedup_key=mainmod._radius_dedup("v1", params_055, "solve"))
    store.finish_job(
        "j4",
        "completed",
        {
            "feasible": False,
            "proven_optimal": True,
            "stop_reason": "completed",
            "lower_bound": 0,
            "nodes_explored": 1,
            "max_depth": 0,
            "gap": None,
            "site_ids": [],
            "uncovered_resident_ids": ["a", "b", "c"],
        },
    )
    return db_path


def _raw_result(db_path, job_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT result_json FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0])


def test_legacy_completed_solve_normalised_on_dedup_and_history(legacy_db):
    # bytes on disk are the old, contradictory shape
    raw = _raw_result(legacy_db, "j1")
    assert raw["proven_optimal"] is True
    assert raw["best_size"] == 2 and raw["lower_bound"] == 1 and raw["gap"] == 1

    with TestClient(mainmod.app) as c:
        # path 1: identical request resubmitted -> reused record, coherent
        r = c.post("/api/versions/v1/solve", json={"radius": 0.6})
        assert r.status_code == 202
        assert r.json()["submit_outcome"] == "reused"
        assert r.json()["job_id"] == "j1"

        got = c.get("/api/jobs/j1").json()
        res = got["result"]
        assert res["proven_optimal"] is True
        assert res["best_size"] == 2
        assert res["lower_bound"] == 2
        assert res["gap"] == 0
        # the plan itself is byte-for-byte the historical one
        assert res["site_ids"] == ["m_ab", "m_bc"]

        # path 2: per-version history listing shows the same coherent view
        rows = c.get("/api/versions/v1/jobs").json()
        by_id = {j["id"]: j for j in rows}
        hres = by_id["j1"]["result"]
        assert hres["lower_bound"] == 2 and hres["gap"] == 0
        assert hres["site_ids"] == ["m_ab", "m_bc"]
        assert hres["best_size"] == 2

        # every completed sweep point obeys the same relation
        sweep = by_id["j2"]["result"]
        assert sweep["points"][0]["lower_bound"] == 2
        assert sweep["points"][0]["gap"] == 0
        assert sweep["points"][0]["best_size"] == 2
        assert sweep["points"][0]["site_ids"] == ["m_ab", "m_bc"]
        assert sweep["points"][1]["lower_bound"] == 1
        assert sweep["points"][1]["gap"] == 0

        # timeout row is left exactly as it was
        timed = by_id["j3"]["result"]
        assert timed["proven_optimal"] is False
        assert timed["lower_bound"] == 1 and timed["gap"] == 2
        assert timed["best_size"] == 3
        assert timed["site_ids"] == ["m_ab", "m_bc", "m_ca"]

        # infeasible row: uncovered list untouched
        infeasible = by_id["j4"]["result"]
        assert infeasible["feasible"] is False
        assert infeasible["uncovered_resident_ids"] == ["a", "b", "c"]

    # read-time normalisation never persisted anything: disk still holds j1
    # as originally written (historical record preserved verbatim)
    raw_after = _raw_result(legacy_db, "j1")
    assert raw_after["lower_bound"] == 1 and raw_after["gap"] == 1
    assert raw_after["site_ids"] == ["m_ab", "m_bc"]


def test_legacy_solution_used_as_incremental_seed(legacy_db):
    with TestClient(mainmod.app) as c:
        # path 3: derive a child version and re-solve incrementally; the
        # contradictory parent row supplies the warm-start plan and the child
        # comes back coherent, without altering the parent's stored plan.
        r = c.post(
            "/api/versions/v1/derive",
            json={"add_residents": [{"id": "d", "x": 0.95, "y": 0.05}]},
        )
        assert r.status_code == 201, r.text
        v2 = r.json()["id"]

        r = c.post(f"/api/versions/{v2}/solve-incremental", json={"radius": 0.6})
        job = wait_for_job(c, r.json()["job_id"])
        assert job["status"] == "completed"
        result = job["result"]
        assert result["feasible"] is True
        assert result["proven_optimal"] is True
        assert result["lower_bound"] == result["best_size"]
        assert result["gap"] == 0
        assert result["incremental"]["warm_started_from_parent"] is True
        assert result["incremental"]["parent_version_id"] == "v1"

        # parent version and its selected plan survive unchanged
        parent = c.get("/api/jobs/j1").json()["result"]
        assert parent["site_ids"] == ["m_ab", "m_bc"]
        assert parent["best_size"] == 2
