"""Geometric primitives and the resident -> candidate coverage relation.

Distance is plain planar Euclidean distance with fixed coordinate units.
A resident ``p`` is served by candidate site ``s`` iff
``euclidean(p, s) <= radius`` (inclusive boundary).

Coverage is keyed by the *resident ids* supplied by the caller, which keeps
cached relations valid even when later versions add/remove residents.
"""

from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence

Point = tuple[float, float]


def euclidean(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _sqdist(a: Point, b: Point) -> float:
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    return dx * dx + dy * dy


def build_coverage(
    residents: Mapping[str, Point] | Sequence[tuple[str, Point]],
    candidates: Mapping[str, Point] | Sequence[tuple[str, Point]],
    radius: float,
) -> dict[str, list[str]]:
    """Return ``{resident_id: [candidate_id, ...]}``.

    ``residents`` / ``candidates`` may be either id->point mappings or
    sequences of ``(id, point)`` pairs (order of the latter is preserved).
    Candidates inside the radius are returned in candidate order.
    """
    res_items = list(residents.items()) if isinstance(residents, Mapping) else list(residents)
    cand_items = list(candidates.items()) if isinstance(candidates, Mapping) else list(candidates)

    r2 = float(radius) * float(radius)
    relation: dict[str, list[str]] = {}
    for rid, rp in res_items:
        covered: list[str] = []
        for cid, cp in cand_items:
            if _sqdist(rp, cp) <= r2:
                covered.append(cid)
        relation[rid] = covered
    return relation


def uncovered_after(
    coverage: Mapping[str, Sequence[str]], open_sites: Iterable[str]
) -> list[str]:
    """Resident ids not served by any of ``open_sites`` (input order)."""
    opened = set(open_sites)
    return [rid for rid, sites in coverage.items() if not opened.intersection(sites)]


def is_feasible(coverage: Mapping[str, Sequence[str]], open_sites: Iterable[str]) -> bool:
    opened = set(open_sites)
    return all(any(s in opened for s in sites) for sites in coverage.values())
