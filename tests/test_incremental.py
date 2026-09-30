"""Incremental re-solve: random add/remove sequences vs cold full solves.

The hard requirement: incremental station count must equal a cold solve of
the same data, and the reported solution must actually cover everyone.
"""

import random

import pytest

from app import incremental, services
from app.solver import exact_set_cover

from .conftest import CLINIC_CANDIDATES, CLINIC_RESIDENTS, submit_and_wait


def test_incremental_matches_cold_on_hand_case(client, clinic_version):
    pid, vid1 = clinic_version
    first = submit_and_wait(client, vid1, {"radius": 5}, path="solve-incremental")
    assert first["result"]["best_size"] == 3
    assert first["result"]["proven_optimal"] is True
    assert first["result"]["incremental"]["reused_coverage"] is False  # no parent

    r = client.post(
        f"/api/versions/{vid1}/derive",
        json={"add_residents": [{"id": "r12", "x": 20.5, "y": 35.5}]},
    )
    vid2 = r.json()["id"]
    inc = submit_and_wait(client, vid2, {"radius": 5}, path="solve-incremental")
    assert inc["result"]["best_size"] == 3
    assert inc["result"]["proven_optimal"] is True
    info = inc["result"]["incremental"]
    assert info["reused_coverage"] is True
    assert info["warm_started_from_parent"] is True
    assert info["fell_back_to_cold"] is False

    # a cold solve of the identical version gives the identical count
    cold = submit_and_wait(client, vid2, {"radius": 5}, path="solve")
    assert cold["result"]["best_size"] == inc["result"]["best_size"]


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_fuzz_incremental_vs_cold(client, seed):
    rng = random.Random(seed)
    # 3x3 lattice of candidates at spacing 10, radius 12: the whole 30x30
    # region (corners at most ~7.1 from a site) is covered by construction, so
    # every add/remove step stays feasible and the feasible-equality path is
    # always exercised (never skipped).
    residents = [
        {"id": f"r{i}", "x": rng.uniform(0, 30), "y": rng.uniform(0, 30)}
        for i in range(14)
    ]
    candidates = [
        {"id": f"c{gx}{gy}", "x": 5 + 10 * gx, "y": 5 + 10 * gy}
        for gx in range(3) for gy in range(3)
    ]
    r = client.post("/api/projects", json={"name": f"fuzz{seed}"})
    pid = r.json()["id"]
    r = client.post(
        f"/api/projects/{pid}/versions",
        json={"residents": residents, "candidates": candidates},
    )
    vid = r.json()["id"]
    radius = 12.0

    current = vid
    # establish a feasible parent
    base = submit_and_wait(client, current, {"radius": radius})
    assert base["result"]["feasible"] is True

    next_new_id = 1000
    for step in range(8):
        v = client.get(f"/api/versions/{current}").json()
        present = [p["id"] for p in v["residents"]]
        body: dict = {}
        if step % 2 == 0 or len(present) <= 4:
            n_add = rng.randint(1, 3)
            added = [
                {"id": f"n{next_new_id + k}", "x": rng.uniform(0, 30), "y": rng.uniform(0, 30)}
                for k in range(n_add)
            ]
            next_new_id += n_add
            body["add_residents"] = added
        if step % 2 == 1 and len(present) > 4:
            body["remove_resident_ids"] = rng.sample(present, rng.randint(1, 2))
        if not body:
            continue
        r = client.post(f"/api/versions/{current}/derive", json=body)
        assert r.status_code == 201, r.text
        current = r.json()["id"]

        inc_job = submit_and_wait(
            client, current, {"radius": radius}, path="solve-incremental"
        )
        # Cold reference straight through the engine (HTTP /solve would be
        # deduplicated against the in-flight incremental job).
        version = client.get(f"/api/versions/{current}").json()
        import threading as _threading

        cold_res = services.solve_version(
            store=client.app.state.store,
            version=version,
            radius=radius,
            forced=[],
            timeout=None,
            cancel_event=_threading.Event(),
        )
        inc_res = inc_job["result"]

        # the lattice guarantees feasibility on every step, so both must be
        # feasible and agree exactly
        assert cold_res["feasible"] is True
        assert inc_res["feasible"] is True

        # THE hard contract: exact same optimum, both proven in these sizes
        assert inc_res["best_size"] == cold_res["best_size"]
        assert inc_res["proven_optimal"] == cold_res["proven_optimal"] is True

        # and the incremental solution must itself achieve full coverage
        relation = services.get_or_build_coverage(
            client.app.state.store, version, radius
        )
        opened = set(inc_res["site_ids"])
        for rid, sites in relation.items():
            assert opened & set(sites), f"{rid} uncovered by incremental solution"


def test_incremental_falls_back_when_candidates_change(client, clinic_version):
    pid, vid1 = clinic_version
    submit_and_wait(client, vid1, {"radius": 5})
    # add an extra candidate in the derived version -> dominance may shift
    new_candidates = [dict(c) for c in CLINIC_CANDIDATES] + [
        {"id": "extra", "x": 20, "y": 17}
    ]
    r = client.post(
        f"/api/versions/{vid1}/derive",
        json={"add_residents": [{"id": "r12", "x": 20, "y": 17}], "candidates": new_candidates},
    )
    assert r.status_code == 201
    vid2 = r.json()["id"]
    inc = submit_and_wait(client, vid2, {"radius": 5}, path="solve-incremental")
    # still exact; only coverage reuse is withheld (candidates changed)
    assert inc["result"]["proven_optimal"] is True
    assert inc["result"]["incremental"]["reused_coverage"] is False
