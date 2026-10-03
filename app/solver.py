"""Exact minimum set cover by branch-and-bound.

The problem is NP-hard, so on large inputs the search may not finish within a
time limit.  The crucial contract enforced here:

* ``proven_optimal`` is ``True`` *only* when the whole search tree was
  exhausted (or closed by a bound).  A greedy incumbent never gets flagged as
  optimal.
* ``lower_bound`` is always a mathematically valid lower bound on the optimum
  (forced sites included), both at the root and whenever the search is
  stopped early.  Therefore ``lower_bound <= optimum <= best_size``.
* When the search completes, the exhausted tree is itself a certificate that
  no smaller cover exists, so ``lower_bound`` is raised to ``best_size``:
  a proven-optimal result always reports ``gap == 0``.

Lower bound
------------
* disjoint-packing: greedily collect residents whose candidate sets are
  pairwise disjoint; each needs a *different* site, so the count is a valid
  lower bound (smallest sets first).

Upper bounds
------------
A set-cover greedy gives the first incumbent (and repairs warm starts).
Branch-and-bound then supplies the certificate of optimality.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

# ---------------------------------------------------------------------------
# Stopping
# ---------------------------------------------------------------------------


class SolveStopped(Exception):
    """Raised internally to unwind the search when time is up / cancelled."""


@dataclass
class StopController:
    deadline: float | None
    cancel_event: threading.Event | None
    checks: int = 0

    def stop_if_requested(self) -> None:
        self.checks += 1
        if self.checks & 2047:
            return
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise SolveStopped
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise SolveStopped


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class SolveResult:
    feasible: bool
    sites: list[str] = field(default_factory=list)
    best_size: int | None = None
    lower_bound: int = 0
    proven_optimal: bool = False
    uncovered: list[str] = field(default_factory=list)
    nodes_explored: int = 0
    max_depth: int = 0
    stop_reason: str = "completed"  # completed | timeout | cancelled

    @property
    def gap(self) -> int | None:
        """best_size - lower_bound (None without an incumbent)."""
        if self.best_size is None:
            return None
        return self.best_size - self.lower_bound


ProgressFn = Callable[[int, int, int, int | None, list[str] | None], None]
# (nodes, depth, lower_bound, best_size, best_sites)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


def _greedy_set_cover(
    resident_ids: Sequence[str],
    cover: dict[str, list[int]],
    allowed: set[int],
    served_by: dict[int, set[str]],
    extra: set[int] | None = None,
) -> list[int] | None:
    """Greedy cover: repeatedly take the candidate covering the most still
    uncovered residents (random-free, ties broken by index). Returns None if
    no allowed combination can cover everyone.
    """
    chosen = {c for c in (extra or ()) if c in allowed}
    unservable = [r for r in resident_ids if not (set(cover[r]) & allowed)]
    if unservable:
        return None
    uncovered = set(resident_ids)
    for c in chosen:
        uncovered -= served_by[c]
    while uncovered:
        best_c: int | None = None
        best_gain = 0
        for c in allowed:
            if c in chosen:
                continue
            gain = len(served_by[c] & uncovered)
            if gain > best_gain:
                best_gain = gain
                best_c = c
        if best_c is None:
            return None
        chosen.add(best_c)
        uncovered -= served_by[best_c]
    return sorted(chosen)


def _packing_lower_bound(
    resident_ids: list[str], cover: dict[str, list[int]]
) -> int:
    """Greedily build a collection of residents with pairwise disjoint
    serving sets; each needs a distinct site. Valid even though greedy.

    Smallest-set-first packs tighter; Timsort in C is also faster here than a
    hand-rolled Python minimum scan (these lists are usually near-sorted).
    """
    used: set[int] = set()
    count = 0
    for r in sorted(resident_ids, key=lambda r: len(cover[r])):
        if used.isdisjoint(cover[r]):
            used.update(cover[r])
            count += 1
    return count


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def exact_set_cover(
    resident_ids: Sequence[str],
    coverage: dict[str, list[str]],
    candidate_ids: Sequence[str],
    forced: Sequence[str] | None = None,
    initial_choice: Sequence[str] | None = None,
    timeout: float | None = None,
    cancel_event: threading.Event | None = None,
    progress: ProgressFn | None = None,
) -> SolveResult:
    """Solve minimum set cover exactly (with optional time/cancel limits).

    Parameters
    ----------
    resident_ids:
        All residents that must be covered, in a stable order.
    coverage:
        ``resident_id -> [candidate_id, ...]`` (only *feasible* pairs).
    candidate_ids:
        Candidate order; indices into this list are used internally.
    forced:
        Candidate ids that must be open.
    initial_choice:
        Feasible warm-start (e.g. previous version's solution). Missing
        coverage is repaired greedily; used only as an initial incumbent.
    timeout:
        Wall-clock seconds; ``None`` means unlimited.
    """
    n = len(candidate_ids)
    index = {cid: i for i, cid in enumerate(candidate_ids)}

    # ---- validate forced against candidate universe ---------------------
    forced_set: set[int] = set()
    for f in forced or ():
        if f not in index:
            raise KeyError(f)
        forced_set.add(index[f])

    # ---- resident sets as candidate-index lists -------------------------
    cover: dict[str, list[int]] = {}
    for rid in resident_ids:
        cover[rid] = [index[c] for c in coverage.get(rid, ()) if c in index]

    # ---- feasibility with all candidates open ---------------------------
    impossible = [r for r in resident_ids if not cover[r]]
    if impossible:
        return SolveResult(
            feasible=False,
            uncovered=impossible,
            lower_bound=0,
            proven_optimal=True,
            stop_reason="completed",
        )

    # ---- apply forced sites ---------------------------------------------
    forced_residual_ids = [r for r in resident_ids if forced_set.isdisjoint(cover[r])]

    # remove candidate-dominated facilities? Build on the *residual* problem.
    # A candidate that covers no residual resident is useless on top of forced.
    serves_resident: dict[int, set[str]] = {c: set() for c in range(n)}
    for r in forced_residual_ids:
        for c in cover[r]:
            serves_resident[c].add(r)

    # dominance: if site a's residual residents are a subset of site b's and
    # a is not forced, a never needs to be opened alongside b.
    dominated: set[int] = set()
    list_cover = [serves_resident[c] for c in range(n)]
    for c in range(n):
        if c in forced_set:
            continue
        si = list_cover[c]
        if not si:
            dominated.add(c)
            continue
        for c2 in range(n):
            if c2 == c or c2 in forced_set:
                continue
            sj = list_cover[c2]
            if len(sj) < len(si):
                continue
            if si < sj or (si == sj and c2 < c):
                dominated.add(c)
                break

    active = [c for c in range(n) if c not in dominated]

    def active_cover_of(r: str) -> list[int]:
        return [c for c in cover[r] if c not in dominated]

    residual_ids = forced_residual_ids
    # after dominance every residual resident must still have an active server
    leftover = [r for r in residual_ids if not active_cover_of(r)]
    if leftover:
        # dominance is safe (a dominator always exists), so this cannot happen;
        # keep guard anyway and undo dominance if it ever did.
        dominated.clear()
        active = list(range(n))

    active_set = set(active)
    active_cover: dict[str, list[int]] = {
        r: active_cover_of(r) for r in residual_ids
    }

    served_by: dict[int, set[str]] = {c: set() for c in active}
    for c in active:
        s = served_by[c]
        for r in residual_ids:
            if c in cover[r]:
                s.add(r)

    # ---- root lower bound -----------------------------------------------
    root_lb = len(forced_set) + _packing_lower_bound(residual_ids, active_cover)

    stop = StopController(deadline=None, cancel_event=cancel_event)
    if timeout is not None:
        stop.deadline = time.monotonic() + max(0.0, float(timeout))

    stats_nodes = 0
    stats_depth_max = 0

    # ---- initial incumbent ----------------------------------------------
    best: list[int] | None = None

    def accept(sites: list[int]) -> bool:
        nonlocal best
        cand = sorted(set(sites) | forced_set)
        if best is None or len(cand) < len(best):
            best = cand
            return True
        return False

    # warm start first (previous feasible solution -> tight early upper bound)
    if initial_choice:
        warm_idx = {index[c] for c in initial_choice if c in index}
        warm_idx |= forced_set
        repaired = _greedy_set_cover(
            residual_ids, active_cover, active_set, served_by, set(warm_idx)
        )
        if repaired is not None:
            accept(repaired)

    greedy = _greedy_set_cover(residual_ids, active_cover, active_set, served_by)
    if greedy is not None:
        accept(greedy)

    # feasibility check at root: residual coverable at all?
    root_infeasible = [r for r in residual_ids if not set(active_cover[r])]
    if root_infeasible:
        return SolveResult(
            feasible=False,
            uncovered=root_infeasible,
            lower_bound=len(forced_set),
            proven_optimal=True,
            stop_reason="completed",
        )

    # already matched the lower bound (or nothing left to cover) -> optimal
    finished_early = False
    if best is not None and len(best) <= root_lb:
        finished_early = True

    stop_reason = "completed"

    def emit() -> None:
        if progress is not None:
            progress(
                stats_nodes,
                stats_depth_max,
                global_lb,
                len(best) if best is not None else None,
                [candidate_ids[c] for c in best] if best is not None else None,
            )

    # The only globally valid lower bound on the *whole* problem is the root
    # bound (forced sites + root packing). A packing bound measured inside a
    # sub-tree after some sites have been excluded is conditional on those
    # exclusions and must NOT be promoted to the global bound -- doing so could
    # overshoot the true optimum. It is still used locally for pruning.
    global_lb = root_lb

    # ---- branch and bound ------------------------------------------------
    # State: residents still uncovered after the sites in ``chosen``; and the
    # set of sites explicitly excluded along this path. Branching is the exact
    # binary decision for one pivot site c: open it (include) or never open it
    # (exclude). Both children are explored -> the search is complete and the
    # optimum cannot be missed.
    chosen: list[int] = []
    excluded: set[int] = set()

    def dfs(remaining: list[str], depth: int) -> None:
        nonlocal stats_nodes, stats_depth_max, finished_early
        stats_nodes += 1
        if depth > stats_depth_max:
            stats_depth_max = depth
        stop.stop_if_requested()

        if not remaining:
            accept(chosen)
            if best is not None and len(best) <= root_lb:
                finished_early = True
                raise SolveStopped  # proven: equals lower bound; unwind cleanly
            return

        used = len(forced_set) + len(chosen)
        if best is not None and used >= len(best):
            return  # cannot beat incumbent even if one site covered all

        # Coverage still available on THIS path (excludes sites ruled out by
        # earlier exclude-decisions). The lower bound MUST be computed from
        # this, never from all active sites, or it can overshoot the optimum.
        avail_cover = {
            r: [c for c in active_cover[r] if c not in excluded] for r in remaining
        }

        # local valid lower bound (conditional on this path's exclusions):
        # packing of remaining residents using only sites still selectable.
        # Used only to prune THIS sub-tree.
        lb = used + _packing_lower_bound(remaining, avail_cover)
        if best is not None and lb >= len(best):
            return

        # choose the hardest remaining resident (fewest still-available sites);
        # tie-break on id for deterministic behaviour
        pivot = remaining[0]
        pivot_options = avail_cover[pivot]
        for r in remaining[1:]:
            opts = avail_cover[r]
            if len(opts) < len(pivot_options) or (
                len(opts) == len(pivot_options) and r < pivot
            ):
                pivot, pivot_options = r, opts
        if not pivot_options:
            return  # exclude-branch made the pivot uncoverable

        # branch site: among the pivot's options, the one covering the most
        # remaining residents is tried first, to find good incumbents early.
        remaining_set = set(remaining)
        c = max(
            pivot_options,
            key=lambda x: (len(served_by[x] & remaining_set), -x),
        )

        # singletons are effectively forced along this path: no exclude child
        # is feasible, so branch include only.
        if len(pivot_options) > 1:
            # child 1: exclude c
            excluded.add(c)
            feasible_exclude = True
            for r in remaining:
                if all(cc in excluded for cc in active_cover[r]):
                    feasible_exclude = False
                    break
            if feasible_exclude:
                dfs(remaining, depth + 1)
            excluded.discard(c)

        # child 2: include c
        if best is not None and used + 1 >= len(best):
            return
        chosen.append(c)
        new_remaining = [r for r in remaining if c not in active_cover[r]]
        dfs(new_remaining, depth + 1)
        chosen.pop()

    try:
        if not finished_early:
            dfs(residual_ids, 0)
    except SolveStopped:
        if cancel_event is not None and cancel_event.is_set():
            stop_reason = "cancelled"
        elif stop.deadline is not None and time.monotonic() >= stop.deadline:
            stop_reason = "timeout"
        elif finished_early:
            stop_reason = "completed"
        else:
            stop_reason = "timeout"
    else:
        stop_reason = "completed"

    proven = stop_reason == "completed" and best is not None
    if best is not None:
        if proven:
            # The search tree was exhausted (or closed early at the root
            # bound): no cover smaller than the incumbent exists.  The
            # incumbent's size is therefore itself a valid -- and the
            # tightest -- lower bound, so a proven result reports gap 0.
            # Reporting only the root packing bound here would understate
            # what the completed search actually proved.
            global_lb = len(best)
        elif global_lb > len(best):
            # root bound must never exceed a feasible incumbent's size; if it
            # did the bound would be invalid, so clamp defensively.
            global_lb = len(best)

    emit()

    if best is None:
        # Search stopped before any feasible incumbent. We must NOT claim the
        # residents are uncoverable: that is only known when the search
        # completed exhaustively (handled earlier as the root-infeasible case).
        if stop_reason == "completed":
            return SolveResult(
                feasible=False,
                uncovered=residual_ids,
                lower_bound=len(forced_set),
                proven_optimal=True,
                nodes_explored=stats_nodes,
                max_depth=stats_depth_max,
                stop_reason="completed",
            )
        return SolveResult(
            feasible=False,
            uncovered=[],
            lower_bound=global_lb,
            proven_optimal=False,
            nodes_explored=stats_nodes,
            max_depth=stats_depth_max,
            stop_reason=stop_reason,
        )

    # verify full coverage explicitly before claiming a feasible solution
    chosen_set = set(best)
    missed = [r for r in resident_ids if chosen_set.isdisjoint(cover[r])]
    if missed:
        # internal error: never report success with missed residents
        return SolveResult(
            feasible=False,
            uncovered=missed,
            lower_bound=global_lb,
            proven_optimal=False,
            nodes_explored=stats_nodes,
            max_depth=stats_depth_max,
            stop_reason=stop_reason,
        )

    return SolveResult(
        feasible=True,
        sites=[candidate_ids[c] for c in best],
        best_size=len(best),
        lower_bound=global_lb,
        proven_optimal=proven,
        nodes_explored=stats_nodes,
        max_depth=stats_depth_max,
        stop_reason=stop_reason,
    )
