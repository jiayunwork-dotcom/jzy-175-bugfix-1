"""Background job scheduling.

Solving can take tens of seconds, so requests return a job id immediately and
the work runs on a small pool of **daemon** worker threads (workers do not
block container shutdown). Each job owns its own :class:`threading.Event`
for cooperative cancellation; the search checks it between branch-and-bound
nodes.

On startup, any job left ``queued``/``running`` by a previous process is
marked ``interrupted`` (its stored result stays available when present).
"""

from __future__ import annotations

import json
import threading
import uuid
from collections import deque
from typing import Any, Callable

from . import store as storemod
from .store import Conflict, Store

# job_id -> cancellation event
_cancel_events: dict[str, threading.Event] = {}
_events_lock = threading.Lock()


class JobManager:
    def __init__(self, store: Store, workers: int = 4):
        self.store = store
        self.workers = max(1, workers)
        self._queue: deque[str] = deque()
        self._cond = threading.Condition()
        self._submit_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {}
        self._threads: list[threading.Thread] = []
        n_interrupted = store.mark_interrupted_on_startup()
        self.interrupted_on_startup = n_interrupted
        for i in range(self.workers):
            t = threading.Thread(target=self._worker_loop, name=f"siting-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def register_handler(self, kind: str, fn: Callable[..., dict[str, Any]]) -> None:
        self._handlers[kind] = fn

    # ------------------------------------------------------------- submission
    def submit(
        self,
        project_id: str,
        version_id: str,
        kind: str,
        params: dict[str, Any],
        dedup_key: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        # Serialise the read-then-(re)queue decision so two identical
        # concurrent posts cannot double-enqueue the same job record.
        with self._submit_lock:
            return self._submit_impl(project_id, version_id, kind, params, dedup_key)

    def _submit_impl(
        self,
        project_id: str,
        version_id: str,
        kind: str,
        params: dict[str, Any],
        dedup_key: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Submit a job, never creating a duplicate record for the same key.

        Returns ``(job_row, outcome)`` where outcome is one of:

        * ``created``      - a brand-new record;
        * ``active``       - an identical queued/running job already exists;
        * ``reused``       - an identical completed job already holds a result;
        * ``rerun``        - an earlier timeout/cancel/interrupt was reset and
                             is queued again **on the same record**.
        """
        if dedup_key is not None:
            existing = self.store.find_by_dedup(dedup_key)
            if existing is not None:
                status = existing["status"]
                if status in ("queued", "running"):
                    return existing, "active"
                if status == "completed":
                    return existing, "reused"
                # timeout | cancelled | interrupted | failed -> re-run in place
                self.store.requeue_job(existing["id"], params)
                with _events_lock:
                    _cancel_events[existing["id"]] = threading.Event()
                with self._cond:
                    self._queue.append(existing["id"])
                    self._cond.notify()
                return self.store.get_job(existing["id"]), "rerun"
        job_id = uuid.uuid4().hex
        try:
            row = self.store.insert_job(
                job_id, project_id, version_id, kind, params, dedup_key=dedup_key
            )
        except Conflict:
            existing = self.store.find_by_dedup(dedup_key)  # type: ignore[arg-type]
            assert existing is not None
            if existing["status"] in ("queued", "running"):
                return existing, "active"
            if existing["status"] == "completed":
                return existing, "reused"
            self.store.requeue_job(existing["id"], params)
            with _events_lock:
                _cancel_events[existing["id"]] = threading.Event()
            with self._cond:
                self._queue.append(existing["id"])
                self._cond.notify()
            return self.store.get_job(existing["id"]), "rerun"
        with _events_lock:
            _cancel_events[job_id] = threading.Event()
        with self._cond:
            self._queue.append(job_id)
            self._cond.notify()
        return row, "created"

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job["status"] in ("queued", "running"):
            with _events_lock:
                ev = _cancel_events.get(job_id)
            if ev is not None:
                ev.set()
            # A still-queued job will observe the event when a worker picks it
            # up; mark it now for prompt feedback.
            if job["status"] == "queued":
                self.store.finish_job(
                    job_id,
                    "cancelled",
                    {
                        "feasible": False,
                        "stop_reason": "cancelled",
                        "note": "cancelled while queued; never executed",
                    },
                )
                with self._cond:
                    try:
                        self._queue.remove(job_id)
                    except ValueError:
                        pass
        return self.store.get_job(job_id)

    # ---------------------------------------------------------------- workers
    def _worker_loop(self) -> None:
        while not self._shutdown.is_set():
            with self._cond:
                while not self._queue and not self._shutdown.is_set():
                    self._cond.wait(timeout=0.5)
                if self._shutdown.is_set():
                    return
                try:
                    job_id = self._queue.popleft()
                except IndexError:
                    continue
            self._run_job(job_id)

    def _run_job(self, job_id: str) -> None:
        # Atomically claim the record. This guards against the same record
        # being dequeued twice (e.g. a stale queue entry after an in-place
        # re-run): only the worker that flips queued -> running executes it.
        if not self.store.claim_queued_job(job_id):
            return
        job = self.store.get_job(job_id)
        with _events_lock:
            event = _cancel_events.get(job_id)
        if event is None:  # should not happen
            event = threading.Event()
            with _events_lock:
                _cancel_events[job_id] = event
        if event.is_set():
            self.store.finish_job(
                job_id,
                "cancelled",
                {"feasible": False, "stop_reason": "cancelled", "note": "cancelled before start"},
            )
            return

        handler = self._handlers.get(job["kind"])
        try:
            assert handler is not None, f"no handler for kind {job['kind']}"

            def progress_cb(progress: dict[str, Any]) -> None:
                self.store.update_job_progress(job_id, progress)

            result = handler(
                job=job,
                store=self.store,
                cancel_event=event,
                progress_cb=progress_cb,
            )
        except Exception as e:  # pragma: no cover - defensive
            self.store.finish_job(
                job_id,
                "failed",
                {"feasible": False, "stop_reason": "failed", "error": repr(e)},
            )
            self.store.set_job_status(job_id, "failed", error=repr(e))
            return

        # Map the engine's stop reason to a terminal job status.
        reason = result.get("stop_reason", "completed")
        status = {
            "completed": "completed",
            "timeout": "timeout",
            "cancelled": "cancelled",
        }.get(reason, "completed")
        self.store.finish_job(job_id, status, result)

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._cond:
            self._cond.notify_all()
