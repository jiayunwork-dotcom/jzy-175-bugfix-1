"""proven_optimal ==> lower_bound == best_size and gap == 0.

Regression for the stale-root-bound bug: after the search tree was exhausted
the reported global lower bound kept the root packing value, so a proven
optimal answer could come back with a positive gap (e.g. best=41, lb=39 on
the 12x12 jittered grid).
"""

from __future__ import annotations

from app import coverage as covmod
from app.solver import exact_set_cover

from .conftest import submit_and_wait
from .test_jobs import _create_grid_version, _grid_payload


# ---------------------------------------------------------------------------
# Engine-level cases
# ---------------------------------------------------------------------------


def _triangle_instance(radius: float):
    """Equilateral-triangle residents, candidate sites at the edge midpoints.

    No single midpoint reaches all three corners, so the optimum is exactly 2,
    while the disjoint packing bound is just 1 (the three 2-element serving
    sets intersect pairwise). This is the smallest geometry in which a stale
    root bound would be visible.
    """
    residents = [
        {"id": "a", "x": 0.0, "y": 0.0},
        {"id": "b", "x": 1.0, "y": 0.0},
        {"id": "c", "x": 0.5, "y": 3 ** 0.5 / 2},
    ]
    candidates = [
        {"id": "m_ab", "x": 0.5, "y": 0.0},
        {"id": "m_bc", "x": 0.75, "y": 3 ** 0.5 / 4},
        {"id": "m_ca", "x": 0.25, "y": 3 ** 0.5 / 4},
    ]
    relation = covmod.build_coverage(
        [(p["id"], (p["x"], p["y"])) for p in residents],
        [(c["id"], (c["x"], c["y"])) for c in candidates],
        radius,
    )
    return residents, candidates, relation


def test_proven_optimal_small_geometry_closes_gap():
    residents, candidates, relation = _triangle_instance(0.6)
    res = exact_set_cover(
        [p["id"] for p in residents],
        relation,
        [c["id"] for c in candidates],
    )
    assert res.proven_optimal is True
    assert res.best_size == 2
    assert res.lower_bound == 2
    assert res.gap == 0
    assert res.stop_reason == "completed"


def test_proven_optimal_grid_closes_gap():
    """The reported instance: optimum 41, root packing used to stop at 39."""
    residents, candidates, r = _grid_payload()
    relation = covmod.build_coverage(
        [(p["id"], (p["x"], p["y"])) for p in residents],
        [(c["id"], (c["x"], c["y"])) for c in candidates],
        r,
    )
    res = exact_set_cover(
        [p["id"] for p in residents],
        relation,
        [c["id"] for c in candidates],
    )
    assert res.proven_optimal is True
    assert res.best_size == 41
    assert res.lower_bound == res.best_size
    assert res.gap == 0


def test_http_completed_grid_solve_is_coherent(client):
    _, vid = _create_grid_version(client)
    job = submit_and_wait(client, vid, {"radius": 1.0}, timeout=120.0)
    assert job["status"] == "completed"
    result = job["result"]
    assert result["proven_optimal"] is True
    assert result["best_size"] == 41
    assert result["lower_bound"] == result["best_size"]
    assert result["gap"] == 0
