"""SQLite persistence for projects, immutable versions, jobs and coverage.

One lightweight SQLite database (WAL) under the data directory.  Versions are
write-once: editing residents/candidates always creates a new version row and
the previous version, with its stored solution, is never mutated.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    version_number  INTEGER NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    residents_json  TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    parent_version_id TEXT,
    change_note     TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    UNIQUE(project_id, version_number)
);
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    version_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,                -- solve | sweep
    params_json   TEXT NOT NULL,
    status        TEXT NOT NULL,                -- queued|running|completed|failed|cancelled|timeout|interrupted
    progress_json TEXT NOT NULL DEFAULT '{}',
    result_json   TEXT,
    error         TEXT,
    dedup_key     TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_version ON jobs(version_id);
-- Exactly one job record per logical request key (version + radius + forced +
-- kind). Sweep jobs pass NULL and are exempt (NULLs are distinct in an index).
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedup
    ON jobs(dedup_key) WHERE dedup_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS coverage_cache (
    version_id    TEXT NOT NULL,
    radius_repr   TEXT NOT NULL,
    relation_json TEXT NOT NULL,
    PRIMARY KEY (version_id, radius_repr)
);
"""


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


class Store:
    """Thread-safe-ish thin wrapper; a module-level lock serialises writes."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self.init_schema()

    def init_schema(self) -> None:
        with connect(self.db_path) as conn:
            conn.executescript(SCHEMA)
            # migrate the older partial unique index (active jobs only) to the
            # full one (one record per logical request key); DROP is idempotent
            # across the two definitions.
            conn.execute("DROP INDEX IF EXISTS idx_jobs_dedup")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedup "
                "ON jobs(dedup_key) WHERE dedup_key IS NOT NULL"
            )

    # --------------------------------------------------------------- projects
    def create_project(self, pid: str, name: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO projects(id, name, created_at) VALUES (?,?,?)",
                (pid, name, now),
            )
        return {"id": pid, "name": name, "created_at": now}

    def get_project(self, pid: str) -> dict[str, Any]:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise NotFound(f"project {pid}")
        return dict(row)

    def list_projects(self) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- versions
    def create_version(
        self,
        version_id: str,
        project_id: str,
        residents: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        parent_version_id: str | None,
        change_note: str,
        label: str = "",
    ) -> dict[str, Any]:
        with self._lock, connect(self.db_path) as conn:
            num = conn.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM versions WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            now = time.time()
            conn.execute(
                """INSERT INTO versions(id, project_id, version_number, label,
                       residents_json, candidates_json, parent_version_id, change_note, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    version_id,
                    project_id,
                    num,
                    label,
                    json.dumps(residents, ensure_ascii=False),
                    json.dumps(candidates, ensure_ascii=False),
                    parent_version_id,
                    change_note,
                    now,
                ),
            )
        return self.get_version(version_id)

    def get_version(self, version_id: str) -> dict[str, Any]:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFound(f"version {version_id}")
        d = dict(row)
        d["residents"] = json.loads(d.pop("residents_json"))
        d["candidates"] = json.loads(d.pop("candidates_json"))
        return d

    def list_versions(self, project_id: str) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM versions WHERE project_id=? ORDER BY version_number",
                (project_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["residents"] = json.loads(d.pop("residents_json"))
            d["candidates"] = json.loads(d.pop("candidates_json"))
            out.append(d)
        return out

    # ------------------------------------------------------------------ jobs
    def insert_job(
        self,
        job_id: str,
        project_id: str,
        version_id: str,
        kind: str,
        params: dict[str, Any],
        dedup_key: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, connect(self.db_path) as conn:
            try:
                conn.execute(
                    """INSERT INTO jobs(id, project_id, version_id, kind, params_json,
                           status, progress_json, dedup_key, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        project_id,
                        version_id,
                        kind,
                        json.dumps(params, ensure_ascii=False),
                        "queued",
                        "{}",
                        dedup_key,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise Conflict(str(e)) from e
        return self.get_job(job_id)

    def find_by_dedup(self, dedup_key: str) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE dedup_key=?",
                (dedup_key,),
            ).fetchone()
        return self._hydrate(row) if row else None

    def requeue_job(self, job_id: str, params: dict[str, Any]) -> None:
        """Reset a finished/stopped job record for re-run in place."""
        with self._lock, connect(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status='queued', params_json=?, progress_json='{}', "
                "result_json=NULL, error=NULL, updated_at=? WHERE id=?",
                (json.dumps(params, ensure_ascii=False), time.time(), job_id),
            )

    def claim_queued_job(self, job_id: str) -> bool:
        """Atomically flip queued -> running. Returns False if another worker
        already claimed it or it is no longer queued."""
        with self._lock, connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='running', updated_at=? "
                "WHERE id=? AND status='queued'",
                (time.time(), job_id),
            )
            return cur.rowcount == 1

    def _hydrate(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["params"] = json.loads(d.pop("params_json"))
        d["progress"] = json.loads(d.pop("progress_json"))
        d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
        return d

    def get_job(self, job_id: str) -> dict[str, Any]:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound(f"job {job_id}")
        return self._hydrate(row)

    def list_jobs(self, version_id: str | None = None) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            if version_id:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE version_id=? ORDER BY created_at",
                    (version_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM jobs ORDER BY created_at").fetchall()
        return [self._hydrate(r) for r in rows]

    def set_job_status(self, job_id: str, status: str, error: str | None = None) -> None:
        with self._lock, connect(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error, time.time(), job_id),
            )

    def update_job_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        with self._lock, connect(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET progress_json=?, updated_at=? WHERE id=?",
                (json.dumps(progress, ensure_ascii=False), time.time(), job_id),
            )

    def finish_job(self, job_id: str, status: str, result: dict[str, Any]) -> None:
        with self._lock, connect(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status=?, result_json=?, updated_at=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False), time.time(), job_id),
            )

    def mark_interrupted_on_startup(self) -> int:
        """Jobs left queued/running by a crashed/restarted process -> interrupted."""
        with self._lock, connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='interrupted', updated_at=? "
                "WHERE status IN ('queued','running')",
                (time.time(),),
            )
            return cur.rowcount

    # --------------------------------------------------------- coverage cache
    def get_coverage(self, version_id: str, radius_repr: str) -> dict[str, list[str]] | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT relation_json FROM coverage_cache WHERE version_id=? AND radius_repr=?",
                (version_id, radius_repr),
            ).fetchone()
        return json.loads(row["relation_json"]) if row else None

    def put_coverage(
        self, version_id: str, radius_repr: str, relation: dict[str, list[str]]
    ) -> None:
        with self._lock, connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO coverage_cache(version_id, radius_repr, relation_json)
                   VALUES (?,?,?)""",
                (version_id, radius_repr, json.dumps(relation, ensure_ascii=False)),
            )

    def copy_coverage(
        self,
        src_version_id: str,
        dst_version_id: str,
        radius_repr: str,
        retained_ids: set[str],
    ) -> dict[str, list[str]] | None:
        rel = self.get_coverage(src_version_id, radius_repr)
        if rel is None:
            return None
        kept = {rid: rel[rid] for rid in retained_ids if rid in rel}
        self.put_coverage(dst_version_id, radius_repr, kept)
        return kept
