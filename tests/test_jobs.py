"""Background jobs: progress, cancel, timeout, dedup, restart, concurrency."""

from __future__ import annotations

import random
import threading
import time

import pytest

from app import coverage as covmod
from app.solver import exact_set_cover

from .conftest import submit_and_wait, wait_for_job

TERMINAL = {"completed", "timeout", "cancelled", "failed", "interrupted"}


# ---------------------------------------------------------------------------
# A geometrically realisable instance that genuinely takes seconds to prove:
# 12x12 grid, jittered points, radius 1.  Full optimum is 41 (verified), and
# a short timeout stops with best=45, lb=45 -> 41 <= 45 <= 45.
# ---------------------------------------------------------------------------


def _grid_payload(seed=1, nside=12, jitter=0.2, r=1.0):
    rng = random.Random(seed)
    residents, candidates = [], []
    for i in range(nside):
        for j in range(nside):
            residents.append(
                {"id": f"r{i}_{j}", "x": i + 0.5 + rng.uniform(-jitter, jitter),
                 "y": j + 0.5 + rng.uniform(-jitter, jitter)}
            )
            candidates.append(
                {"id": f"c{i}_{j}", "x": i + rng.uniform(-jitter, jitter),
                 "y": j + rng.uniform(-jitter, jitter)}
            )
    return residents, candidates, r


def _create_grid_version(client):
    residents, candidates, r = _grid_payload()
    pr = client.post("/api/projects", json={"name": "grid"})
    pid = pr.json()["id"]
    vr = client.post(
        f"/api/projects/{pid}/versions",
        json={"residents": residents, "candidates": candidates},
    )
    return pid, vr.json()["id"]


# --------------------------------------------------------------- engine-level
def test_engine_timeout_sandwich():
    """lb <= true optimum <= reported best; status is timeout, not completed."""
    residents, candidates, r = _grid_payload()
    relation = covmod.build_coverage(
        [(p["id"], (p["x"], p["y"])) for p in residents],
        [(c["id"], (c["x"], c["y"])) for c in candidates],
        r,
    )
    full = exact_set_cover(
        [p["id"] for p in residents], relation, [c["id"] for c in candidates]
    )
    assert full.proven_optimal is True
    optimum = full.best_size

    timed = exact_set_cover(
        [p["id"] for p in residents],
        relation,
        [c["id"] for c in candidates],
        timeout=0.05,
    )
    assert timed.stop_reason == "timeout"
    assert timed.proven_optimal is False
    assert timed.best_size is not None
    assert timed.lower_bound <= optimum <= timed.best_size

    # and the timeout incumbent really does cover everyone
    opened = set(timed.sites)
    assert all(opened & set(v) for v in relation.values())


def test_engine_cancel_is_cooperative_and_honest():
    residents, candidates, r = _grid_payload()
    relation = covmod.build_coverage(
        [(p["id"], (p["x"], p["y"])) for p in residents],
        [(c["id"], (c["x"], c["y"])) for c in candidates],
        r,
    )
    event = threading.Event()
    event.set()  # cancel before the first node batch is even processed
    out = exact_set_cover(
        [p["id"] for p in residents],
        relation,
        [c["id"] for c in candidates],
        cancel_event=event,
    )
    assert out.stop_reason == "cancelled"
    assert out.proven_optimal is False


# ------------------------------------------------------------------ HTTP jobs
def test_http_progress_cancel_and_timeout(client):
    _, vid = _create_grid_version(client)

    # 1) progress is observable while running, then cancellation lands
    r = client.post(f"/api/versions/{vid}/solve", json={"radius": 1.0})
    long_job = r.json()["job_id"]
    saw_running = False
    deadline = time.time() + 4
    while time.time() < deadline:
        j = client.get(f"/api/jobs/{long_job}").json()
        if j["status"] == "running":
            saw_running = True
            assert j["progress"].get("nodes_explored", 0) >= 0
            assert j["progress"].get("lower_bound", 0) >= 0
            cancel = client.post(f"/api/jobs/{long_job}/cancel")
            assert cancel.status_code == 200
            break
        if j["status"] in TERMINAL:
            break
        time.sleep(0.01)
    final = wait_for_job(client, long_job)
    assert saw_running, "job never observed running"
    assert final["status"] == "cancelled", final["status"]
    assert final["result"]["stop_reason"] == "cancelled"
    assert final["result"].get("proven_optimal") is False

    # 2) a separately submitted job with a tight deadline times out and hands
    #    back best + lb + gap, marked timeout (never completed)
    r = client.post(f"/api/versions/{vid}/solve", json={"radius": 1.0, "timeout": 0.05})
    j = wait_for_job(client, r.json()["job_id"])
    assert j["status"] == "timeout"
    res = j["result"]
    assert res["stop_reason"] == "timeout"
    assert res["proven_optimal"] is False
    assert res["best_size"] >= 41
    assert res["lower_bound"] <= 41
    assert res["gap"] == res["best_size"] - res["lower_bound"]


def test_identical_request_deduplicates(client):
    _, vid = _create_grid_version(client)

    # a fast, provable solve so it completes quickly
    r1 = client.post(f"/api/versions/{vid}/solve", json={"radius": 1.1})
    j1 = r1.json()
    assert j1["submit_outcome"] == "created"
    job = wait_for_job(client, j1["job_id"])
    assert job["status"] == "completed"

    # identical request after completion -> same record, result reused
    r2 = client.post(f"/api/versions/{vid}/solve", json={"radius": 1.1})
    j2 = r2.json()
    assert j2["submit_outcome"] == "reused"
    assert j2["job_id"] == j1["job_id"]
    assert j2["deduplicated"] is True

    # different forced sites -> different record
    r3 = client.post(
        f"/api/versions/{vid}/solve",
        json={"radius": 1.1, "forced_site_ids": ["c0_0"]},
    )
    j3 = r3.json()
    assert j3["submit_outcome"] == "created"
    wait_for_job(client, j3["job_id"])

    # exactly two solve records for this version (no duplicates)
    rows = client.get(f"/api/versions/{vid}/jobs").json()
    solves = [j for j in rows if j["kind"] == "solve"]
    keys = {(j["params"]["radius"], tuple(j["params"].get("forced_site_ids", []))) for j in solves}
    assert len(solves) == len(keys) == 2


def test_timed_out_job_reruns_in_place(client):
    _, vid = _create_grid_version(client)
    r1 = client.post(
        f"/api/versions/{vid}/solve", json={"radius": 1.0, "timeout": 0.05}
    )
    jid = r1.json()["job_id"]
    j = wait_for_job(client, jid)
    assert j["status"] == "timeout"

    # same request again -> same record re-queued (rerun), no duplicate
    r2 = client.post(f"/api/versions/{vid}/solve", json={"radius": 1.0, "timeout": 0.05})
    assert r2.json()["submit_outcome"] == "rerun"
    assert r2.json()["job_id"] == jid
    j2 = wait_for_job(client, jid)
    assert j2["status"] == "timeout"
    rows = client.get(f"/api/versions/{vid}/jobs").json()
    assert [j["id"] for j in rows if j["kind"] == "solve"].count(jid) == 1


def test_concurrent_jobs_do_not_interfere(client):
    _, vid = _create_grid_version(client)
    j1 = client.post(f"/api/versions/{vid}/solve", json={"radius": 1.0, "timeout": 5}).json()["job_id"]
    j2 = client.post(f"/api/versions/{vid}/sweep", json={"radii": [0.5, 1.0]}).json()["job_id"]
    r1 = wait_for_job(client, j1)
    r2 = wait_for_job(client, j2)
    assert r1["id"] == j1 and r2["id"] == j2
    # each reports its own coherent result
    assert r1["result"]["stop_reason"] in ("completed", "timeout", "cancelled")
    assert r2["kind"] == "sweep"
    assert len(r2["result"]["points"]) >= 1


def test_restart_marks_unfinished_jobs_interrupted_and_keeps_results(client, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main as mainmod

    _, vid = _create_grid_version(client)
    # a quick completed feasible job whose result must survive restart
    done = submit_and_wait(client, vid, {"radius": 1.1})
    assert done["status"] == "completed"
    assert done["result"]["feasible"] is True

    # simulate a job left RUNNING by a crashed process
    store = client.app.state.store
    import uuid
    store.insert_job(uuid.uuid4().hex, store.get_version(vid)["project_id"], vid, "solve",
                     {"radius": 1.0}, dedup_key=None)
    row = store.list_jobs(vid)[-1]
    store.set_job_status(row["id"], "running")

    # fresh process on the same database -> interrupted, never stuck running
    monkeypatch.setattr(mainmod, "WORKERS", 2)
    with TestClient(mainmod.app) as c2:
        j = c2.get(f"/api/jobs/{row['id']}").json()
        assert j["status"] == "interrupted"
        kept = c2.get(f"/api/jobs/{done['id']}").json()
        assert kept["status"] == "completed"
        assert kept["result"]["feasible"] is True
