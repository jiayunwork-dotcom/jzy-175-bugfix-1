"""Incremental re-solving after residents are added/removed on a version.

Chosen strategy: **reuse the coverage relation + warm-start the exact
branch-and-bound with the parent version's best feasible solution, then still
run the full optimality proof.**

Why this strategy
-----------------
The hard part of site selection is the combinatorial search, not the geometry.
When only residents change (candidate sites and radius stay the same):

* the coverage relation for every retained resident is byte-for-byte the
  same, so it is copied instead of recomputed; new residents alone are
  measured geometrically;
* the parent solution still covers every retained resident, so it either
  remains feasible (removals only) or needs a small greedy repair (additions).
  Seeding branch-and-bound with that incumbent prunes hard immediately.

Critically, warm-starting only supplies an *upper bound*. The branch-and-bound
search is still run to completion with valid lower bounds, so a returned
solution is marked optimal exactly when the tree is exhausted. Hence the
incremental answer cannot differ from a cold full solve: both return the same
optimum and ``proven_optimal`` is derived identically. A timeout/cancel leaves
the same honest status as a cold solve.

When the reuse conditions do not hold we transparently fall back to a cold
solve (``incremental_reused`` is reported so the choice is visible):

* candidate sites changed in the new version (dominance structure changes);
* radius differs from the parent job's radius (coverage must be rebuilt);
* no feasible parent solution exists to seed from.

Coverage reuse itself is independent: the copied relation is exact under the
unchanged (candidates, radius) pair regardless of fallback.
"""

from __future__ import annotations

from typing import Any, Callable

from . import coverage as covmod
from .services import points_map, solve_version, validate_solve_params


def build_derived_coverage(
    store,
    parent_version: dict[str, Any],
    new_version: dict[str, Any],
    radius: float,
) -> tuple[dict[str, list[str]], bool]:
    """Return ``(relation, reused)`` for a derived version.

    Coverage entries of retained residents are copied verbatim from the
    parent's cached relation; added residents are computed.
    """
    radius_repr = repr(float(radius))
    parent_ids = {p["id"] for p in parent_version["residents"]}
    new_residents = new_version["residents"]
    new_candidates = new_version["candidates"]
    candidates_changed = [c["id"] for c in new_candidates] != [
        c["id"] for c in parent_version["candidates"]
    ]

    relation: dict[str, list[str]] = {}
    reused = False
    if not candidates_changed:
        retained = {p["id"] for p in new_residents} & parent_ids
        copied = store.copy_coverage(
            parent_version["id"], new_version["id"], radius_repr, retained
        )
        if copied is not None:
            relation.update(copied)
            reused = True

    new_points = points_map(new_residents)
    cand_points = points_map(new_candidates)
    missing_res = [p for p in new_residents if p["id"] not in relation]
    if missing_res:
        computed = covmod.build_coverage(
            [(p["id"], new_points[p["id"]]) for p in missing_res],
            list(cand_points.items()),
            radius,
        )
        relation.update(computed)

    # ensure stable canonical order matching the new version's residents
    relation = {p["id"]: relation[p["id"]] for p in new_residents}
    store.put_coverage(new_version["id"], radius_repr, relation)
    return relation, reused


def _parent_seed_job(
    store, parent_version_id: str, radius: float, forced: list[str]
) -> dict[str, Any] | None:
    """Find the parent's most recent feasible solve at this radius with the
    same forced sites (prefer proven optimal). Both plain and incremental
    solves are valid parents -- they hold exact, fully-covered solutions."""
    jobs = store.list_jobs(parent_version_id)
    candidates: list[dict[str, Any]] = []
    for j in jobs:
        if j["kind"] not in ("solve", "incremental") or j["result"] is None:
            continue
        p = j["params"]
        if repr(float(p.get("radius", -1))) != repr(float(radius)):
            continue
        if list(p.get("forced_site_ids", [])) != list(forced):
            continue
        r = j["result"]
        if r.get("feasible") and r.get("site_ids"):
            candidates.append(j)
    if not candidates:
        return None
    candidates.sort(key=lambda j: (not j["result"].get("proven_optimal"), j["created_at"]))
    return candidates[0]


def incremental_solve(
    *,
    job: dict[str, Any],
    store,
    cancel_event,
    progress_cb: Callable[[dict[str, Any]], None],
    version_loader: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Job handler: derive coverage, seed from parent, then solve exactly."""
    from .services import run_solve

    params = job["params"]
    version = version_loader(job["version_id"])
    candidate_ids = {c["id"] for c in version["candidates"]}
    radius, forced, timeout = validate_solve_params(params, candidate_ids)

    relation: dict[str, list[str]]
    reused_coverage = False
    seed: list[str] | None = None
    used_seed = False
    parent_id = version.get("parent_version_id")

    if parent_id:
        parent = version_loader(parent_id)
        candidates_changed = [c["id"] for c in version["candidates"]] != [
            c["id"] for c in parent["candidates"]
        ]
        relation, reused_coverage = build_derived_coverage(store, parent, version, radius)
        if not candidates_changed:
            parent_job = _parent_seed_job(store, parent_id, radius, forced)
            if parent_job is not None:
                seed = parent_job["result"]["site_ids"]
                used_seed = True
    else:  # defensive: non-derived version behaves like a normal solve
        from .services import get_or_build_coverage

        relation = get_or_build_coverage(store, version, radius)

    result = run_solve(
        version=version,
        relation=relation,
        radius=radius,
        forced=forced,
        timeout=timeout,
        cancel_event=cancel_event,
        initial_site_ids=seed,
        progress_cb=progress_cb,
    )
    result["incremental"] = {
        "reused_coverage": reused_coverage,
        "warm_started_from_parent": used_seed,
        "fell_back_to_cold": not used_seed,
        "parent_version_id": parent_id,
    }
    return result
