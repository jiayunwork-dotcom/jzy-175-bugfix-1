"""Validation and the solving engines (single solve + radius sweep).

Coordinates coverage geometry, the exact solver, the coverage cache and the
job store.  Incremental logic lives in :mod:`app.incremental`.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Sequence

from . import coverage as covmod
from .solver import SolveResult, exact_set_cover


class ValidationError(Exception):
    """Field-anchored validation failure: ``errors`` maps field -> message."""

    def __init__(self, errors: list[dict[str, str]]):
        super().__init__("; ".join(f"{e['field']}: {e['message']}" for e in errors))
        self.errors = errors


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_points(items: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    errors: list[dict[str, str]] = []
    if not isinstance(items, list) or not items:
        raise ValidationError(
            [{"field": kind, "message": f"{kind} must be a non-empty list"}]
        )
    seen: set[str] = set()
    for i, item in enumerate(items):
        prefix = f"{kind}[{i}]"
        if not isinstance(item, dict):
            errors.append({"field": prefix, "message": "must be an object"})
            continue
        pid = item.get("id")
        if not isinstance(pid, str) or not pid:
            errors.append({"field": f"{prefix}.id", "message": "id is required and must be a string"})
        elif pid in seen:
            errors.append({"field": f"{prefix}.id", "message": f"duplicate id {pid!r}"})
        else:
            seen.add(pid)
        for ax in ("x", "y"):
            v = item.get(ax)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                errors.append({"field": f"{prefix}.{ax}", "message": f"{ax} must be a finite number"})
    if errors:
        raise ValidationError(errors)
    return items


def validate_version_payload(payload: dict[str, Any], partial: bool = False) -> None:
    residents = payload.get("residents")
    candidates = payload.get("candidates")
    if residents is not None:
        _validate_points(residents, "residents")
    elif not partial:
        raise ValidationError([{"field": "residents", "message": "field required"}])
    if candidates is not None:
        _validate_points(candidates, "candidates")
    elif not partial:
        raise ValidationError([{"field": "candidates", "message": "field required"}])


def validate_solve_params(
    params: dict[str, Any], candidate_ids: set[str]
) -> tuple[float, list[str], float | None]:
    errors: list[dict[str, str]] = []
    radius = params.get("radius")
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or radius <= 0:
        errors.append({"field": "radius", "message": "radius must be a positive number"})
    forced = params.get("forced_site_ids", []) or []
    if not isinstance(forced, list) or not all(isinstance(f, str) for f in forced):
        errors.append({"field": "forced_site_ids", "message": "must be a list of site ids"})
        forced = []
    missing = [f for f in forced if f not in candidate_ids]
    if missing:
        errors.append(
            {
                "field": "forced_site_ids",
                "message": f"unknown site id(s): {', '.join(missing)}",
            }
        )
    timeout = params.get("timeout")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            errors.append({"field": "timeout", "message": "timeout must be a positive number when given"})
            timeout = None
    if errors:
        raise ValidationError(errors)
    return float(radius), list(dict.fromkeys(forced)), (float(timeout) if timeout is not None else None)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def points_map(items: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    return {p["id"]: (float(p["x"]), float(p["y"])) for p in items}


def get_or_build_coverage(store, version: dict[str, Any], radius: float) -> dict[str, list[str]]:
    """Coverage by resident id; cached per (version, radius)."""
    radius_repr = repr(float(radius))
    cached = store.get_coverage(version["id"], radius_repr)
    if cached is not None:
        # cache could predate newly computed residents? versions are immutable
        # so a cache hit is complete.
        return cached
    relation = covmod.build_coverage(
        points_map(version["residents"]), points_map(version["candidates"]), radius
    )
    store.put_coverage(version["id"], radius_repr, relation)
    return relation


# ---------------------------------------------------------------------------
# Single-solve engine
# ---------------------------------------------------------------------------


def _result_to_dict(res: SolveResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "feasible": res.feasible,
        "proven_optimal": res.proven_optimal,
        "stop_reason": res.stop_reason,
        "lower_bound": res.lower_bound,
        "nodes_explored": res.nodes_explored,
        "max_depth": res.max_depth,
        "gap": res.gap,
    }
    if res.feasible:
        out["site_ids"] = res.sites
        out["best_size"] = res.best_size
    else:
        out["site_ids"] = []
        out["uncovered_resident_ids"] = res.uncovered
    return out


def run_solve(
    version: dict[str, Any],
    relation: dict[str, list[str]],
    radius: float,
    forced: Sequence[str],
    timeout: float | None,
    cancel_event: threading.Event,
    initial_site_ids: Sequence[str] | None = None,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the exact solver against a prebuilt coverage relation."""
    resident_ids = [p["id"] for p in version["residents"]]
    candidate_ids = [c["id"] for c in version["candidates"]]

    last_emit = 0.0

    def on_progress(nodes: int, depth: int, lb: int, best: int | None, sites: list[str] | None):
        nonlocal last_emit
        now = time.monotonic()
        if progress_cb is not None and (now - last_emit >= 0.25 or best is not None):
            last_emit = now
            progress_cb(
                {
                    "nodes_explored": nodes,
                    "search_depth": depth,
                    "lower_bound": lb,
                    "best_size": best,
                    "best_site_ids": sites or [],
                }
            )

    res = exact_set_cover(
        resident_ids=resident_ids,
        coverage=relation,
        candidate_ids=candidate_ids,
        forced=list(forced),
        initial_choice=list(initial_site_ids) if initial_site_ids else None,
        timeout=timeout,
        cancel_event=cancel_event,
        progress=on_progress,
    )
    return _result_to_dict(res)


def solve_version(
    store,
    version: dict[str, Any],
    radius: float,
    forced: Sequence[str],
    timeout: float | None,
    cancel_event: threading.Event,
    initial_site_ids: Sequence[str] | None = None,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Build/fetch coverage then solve."""
    relation = get_or_build_coverage(store, version, radius)
    return run_solve(
        version=version,
        relation=relation,
        radius=radius,
        forced=forced,
        timeout=timeout,
        cancel_event=cancel_event,
        initial_site_ids=initial_site_ids,
        progress_cb=progress_cb,
    )


# ---------------------------------------------------------------------------
# Radius sweep engine
# ---------------------------------------------------------------------------


def run_sweep(
    store,
    version: dict[str, Any],
    radii: list[float],
    forced: Sequence[str],
    timeout: float | None,
    cancel_event: threading.Event,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Sweep strictly ascending radii. Each step warm-starts from the previous
    (smaller-radius) solution, which remains feasible at the larger radius."""
    started = time.monotonic()
    points: list[dict[str, Any]] = []
    seed: list[str] | None = None
    for i, r in enumerate(radii):
        if cancel_event.is_set():
            return {
                "feasible": all(p["feasible"] for p in points),
                "points": points,
                "completed_steps": i,
                "total_steps": len(radii),
                "stop_reason": "cancelled",
            }
        step_timeout: float | None = None
        if timeout is not None:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                return {
                    "feasible": all(p["feasible"] for p in points),
                    "points": points,
                    "completed_steps": i,
                    "total_steps": len(radii),
                    "stop_reason": "timeout",
                }
            step_timeout = remaining

        def step_progress(pr: dict[str, Any], _i=i):
            if progress_cb is not None:
                progress_cb({"step": _i, "total_steps": len(radii), **pr})

        result = solve_version(
            store=store,
            version=version,
            radius=r,
            forced=forced,
            timeout=step_timeout,
            cancel_event=cancel_event,
            initial_site_ids=seed,
            progress_cb=step_progress,
        )
        point = {"radius": r, **result}
        points.append(point)
        if result["feasible"]:
            seed = result["site_ids"]
        if result["stop_reason"] in ("timeout", "cancelled"):
            return {
                "feasible": all(p["feasible"] for p in points),
                "points": points,
                "completed_steps": i + 1,
                "total_steps": len(radii),
                "stop_reason": result["stop_reason"],
            }
    return {
        "feasible": all(p["feasible"] for p in points),
        "points": points,
        "completed_steps": len(radii),
        "total_steps": len(radii),
        "stop_reason": "completed",
    }
