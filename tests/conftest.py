"""Shared fixtures: isolated data dir per test, API client, polling helper."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import main as mainmod

TERMINAL_STATUSES = {"completed", "timeout", "cancelled", "failed", "interrupted"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "siting.db")
    monkeypatch.setattr(mainmod, "DB_PATH", db_path)
    monkeypatch.setattr(mainmod, "WORKERS", 2)
    with TestClient(mainmod.app) as c:
        c._test_db_path = db_path  # type: ignore[attr-defined]
        yield c


def wait_for_job(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while True:
        r = client.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200
        body = r.json()
        if body["status"] in TERMINAL_STATUSES:
            return body
        assert time.time() < deadline, f"job {job_id} still {body['status']}"
        time.sleep(0.02)


def submit_and_wait(client, version_id, payload, path="solve", timeout=30.0):
    r = client.post(f"/api/versions/{version_id}/{path}", json=payload)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    return wait_for_job(client, job_id, timeout=timeout)


# ---------------------------------------------------------------------------
# Hand-verifiable clinic regression case.
#
# Three well-separated residential clusters (~40 units apart), one clinic site
# inside each cluster, plus two decoy sites that cover nobody at radius 5:
#
#   cluster A around (0,0)    : r1 r2 r3 r4   -> only c1 can serve
#   cluster B around (40,0)   : r5 r6 r7 r8   -> only c2 can serve
#   cluster C around (20,35)  : r9 r10 r11    -> only c3 can serve
#
# Any feasible solution must therefore open c1 AND c2 AND c3 (three pairwise
# disjoint required groups) -> optimum is exactly 3, easy to verify by hand.
# ---------------------------------------------------------------------------

CLINIC_RESIDENTS = [
    {"id": "r1", "x": 0, "y": 0},
    {"id": "r2", "x": 1, "y": 0.5},
    {"id": "r3", "x": 0.5, "y": -1},
    {"id": "r4", "x": -1, "y": 1},
    {"id": "r5", "x": 40, "y": 0},
    {"id": "r6", "x": 41, "y": 1},
    {"id": "r7", "x": 39, "y": -0.5},
    {"id": "r8", "x": 40.5, "y": -1},
    {"id": "r9", "x": 20, "y": 35},
    {"id": "r10", "x": 21, "y": 36},
    {"id": "r11", "x": 19, "y": 34.5},
]

CLINIC_CANDIDATES = [
    {"id": "c1", "x": 0, "y": 0},
    {"id": "c2", "x": 40, "y": 0},
    {"id": "c3", "x": 20, "y": 35},
    {"id": "d1", "x": 20, "y": 0},    # decoy in the middle, covers nobody
    {"id": "d2", "x": 10, "y": 17},   # decoy, covers nobody
]


@pytest.fixture
def clinic_version(client):
    r = client.post("/api/projects", json={"name": "clinic"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = client.post(
        f"/api/projects/{pid}/versions",
        json={"residents": CLINIC_RESIDENTS, "candidates": CLINIC_CANDIDATES},
    )
    assert r.status_code == 201, r.text
    return pid, r.json()["id"]
