"""HTTP API for community service-station siting.

Only a JSON HTTP interface is exposed (no UI). Modules stay separate:

* coverage geometry  -> app/coverage.py
* exact optimisation -> app/solver.py
* persistence         -> app/store.py
* background jobs     -> app/jobs.py
* versions/increment  -> app/incremental.py
* orchestration       -> app/services.py
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import incremental, services
from .jobs import JobManager
from .services import ValidationError
from .store import NotFound, Store

DATA_DIR = os.environ.get("SITING_DATA_DIR", "/data")
DB_PATH = os.environ.get("SITING_DB_PATH", os.path.join(DATA_DIR, "siting.db"))
WORKERS = int(os.environ.get("SITING_WORKERS", "4"))


# ---------------------------------------------------------------------------
# Error envelope: every field error points at the offending field.
# ---------------------------------------------------------------------------


def error_response(status: int, code: str, message: str, errors=None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message}}
    if errors:
        body["error"]["errors"] = errors
    return JSONResponse(status_code=status, content=body)


async def _json(request: Request) -> dict:
    """Parse a JSON object body; malformed payloads are field-anchored 400s."""
    try:
        payload = await request.json()
    except Exception:
        raise ValidationError([{"field": "body", "message": "request body must be valid JSON"}])
    if not isinstance(payload, dict):
        raise ValidationError([{"field": "body", "message": "request body must be a JSON object"}])
    return payload


# ---------------------------------------------------------------------------
# Job handlers (run on worker threads)
# ---------------------------------------------------------------------------


def _handle_solve(*, job, store, cancel_event, progress_cb, version_loader):
    params = job["params"]
    version = version_loader(job["version_id"])
    candidate_ids = {c["id"] for c in version["candidates"]}
    radius, forced, timeout = services.validate_solve_params(params, candidate_ids)
    return services.solve_version(
        store=store,
        version=version,
        radius=radius,
        forced=forced,
        timeout=timeout,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
    )


def _handle_sweep(*, job, store, cancel_event, progress_cb, version_loader):
    params = job["params"]
    version = version_loader(job["version_id"])
    radii = sorted(float(r) for r in params["radii"])
    forced = params.get("forced_site_ids", []) or []
    timeout = params.get("timeout")
    return services.run_sweep(
        store=store,
        version=version,
        radii=radii,
        forced=forced,
        timeout=timeout,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
    )


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store(DB_PATH)
    manager = JobManager(store, workers=WORKERS)

    def version_loader(version_id: str):
        return store.get_version(version_id)

    manager.register_handler(
        "solve",
        lambda **kw: _handle_solve(**kw, version_loader=version_loader),
    )
    manager.register_handler(
        "sweep",
        lambda **kw: _handle_sweep(**kw, version_loader=version_loader),
    )
    manager.register_handler(
        "incremental",
        lambda **kw: incremental.incremental_solve(
            **kw, version_loader=version_loader
        ),
    )
    app.state.store = store
    app.state.manager = manager
    yield
    manager.shutdown()


app = FastAPI(
    title="社区服务站选址后端",
    version="1.0.0",
    description="Exact minimum set cover for service-station siting, with versions and background jobs.",
    lifespan=lifespan,
)


@app.exception_handler(ValidationError)
async def _validation_handler(request: Request, exc: ValidationError):
    return error_response(400, "validation_error", "请求参数不合法", exc.errors)


@app.exception_handler(NotFound)
async def _notfound_handler(request: Request, exc: NotFound):
    return error_response(404, "not_found", str(exc))


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- projects --


@app.post("/api/projects", status_code=201)
async def create_project(request: Request):

    payload = await _json(request)
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError(
            [{"field": "name", "message": "name is required and must be a non-empty string"}]
        )
    store: Store = request.app.state.store
    pid = uuid.uuid4().hex
    store.create_project(pid, name.strip())
    return {"id": pid, "name": name.strip()}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, request: Request):
    return request.app.state.store.get_project(project_id)


@app.get("/api/projects")
def list_projects(request: Request):
    return request.app.state.store.list_projects()


# --------------------------------------------------------------- versions --


def _serialize_version(v: dict) -> dict:
    return {
        "id": v["id"],
        "project_id": v["project_id"],
        "version_number": v["version_number"],
        "label": v["label"],
        "residents": v["residents"],
        "candidates": v["candidates"],
        "parent_version_id": v["parent_version_id"],
        "change_note": v["change_note"],
        "created_at": v["created_at"],
    }


@app.post("/api/projects/{project_id}/versions", status_code=201)
async def create_version(project_id: str, request: Request):
    payload = await _json(request)
    store: Store = request.app.state.store
    store.get_project(project_id)  # 404 if missing
    services.validate_version_payload(payload)
    vid = uuid.uuid4().hex
    v = store.create_version(
        version_id=vid,
        project_id=project_id,
        residents=payload["residents"],
        candidates=payload["candidates"],
        parent_version_id=None,
        change_note=payload.get("change_note", ""),
        label=payload.get("label", ""),
    )
    return _serialize_version(v)


@app.get("/api/versions/{version_id}")
def get_version(version_id: str, request: Request):
    return _serialize_version(request.app.state.store.get_version(version_id))


@app.get("/api/projects/{project_id}/versions")
def list_versions(project_id: str, request: Request):
    store: Store = request.app.state.store
    store.get_project(project_id)
    return [_serialize_version(v) for v in store.list_versions(project_id)]


@app.post("/api/versions/{version_id}/derive", status_code=201)
async def derive_version(version_id: str, request: Request):
    """Create a new version by adding/removing residents.

    Body: {"add_residents": [...], "remove_resident_ids": [...], "change_note": ...}
    Candidate sites can be replaced wholesale as well ("candidates").
    """
    payload = await _json(request)
    store: Store = request.app.state.store
    parent = store.get_version(version_id)

    add = payload.get("add_residents", []) or []
    remove = set(payload.get("remove_resident_ids", []) or [])
    if not isinstance(add, list) or not all(isinstance(x, dict) for x in add):
        raise ValidationError([{"field": "add_residents", "message": "must be a list of point objects"}])
    if not all(isinstance(x, str) for x in remove):
        raise ValidationError([{"field": "remove_resident_ids", "message": "must be a list of ids"}])

    existing = {p["id"]: p for p in parent["residents"]}
    unknown = [rid for rid in remove if rid not in existing]
    if unknown:
        raise ValidationError(
            [{"field": "remove_resident_ids", "message": f"unknown resident id(s): {', '.join(sorted(unknown))}"}]
        )
    add_payload = {"residents": add} if add else None
    if add:
        services.validate_version_payload(add_payload, partial=True)
    add_ids = [p["id"] for p in add]
    clash = [rid for rid in add_ids if rid in existing and rid not in remove]
    if clash:
        raise ValidationError(
            [{"field": "add_residents", "message": f"id(s) already present: {', '.join(clash)}"}]
        )

    kept = [p for p in parent["residents"] if p["id"] not in remove]
    new_residents = kept + add

    candidates = payload.get("candidates")
    if candidates is not None:
        services.validate_version_payload({"residents": new_residents, "candidates": candidates})
        new_candidates = candidates
    else:
        new_candidates = parent["candidates"]

    if not new_residents:
        raise ValidationError([{"field": "residents", "message": "derived version would have no residents"}])

    note = payload.get("change_note", "")
    vid = uuid.uuid4().hex
    v = store.create_version(
        version_id=vid,
        project_id=parent["project_id"],
        residents=new_residents,
        candidates=new_candidates,
        parent_version_id=parent["id"],
        change_note=note,
        label=payload.get("label", ""),
    )
    return _serialize_version(v)


# ------------------------------------------------------------------ solve --


def _radius_dedup(version_id: str, params: dict, kind: str) -> str:
    forced = sorted(params.get("forced_site_ids", []) or [])
    return f"{kind}:{version_id}:{repr(float(params['radius']))}:{','.join(forced)}"


@app.post("/api/versions/{version_id}/solve", status_code=202)
async def submit_solve(version_id: str, request: Request):
    payload = await _json(request)
    store: Store = request.app.state.store
    version = store.get_version(version_id)
    # raises 400 with field-anchored errors on bad radius / unknown forced ids
    radius, forced, timeout = services.validate_solve_params(
        payload, {c["id"] for c in version["candidates"]}
    )
    params = {"radius": radius, "forced_site_ids": forced}
    if timeout is not None:
        params["timeout"] = timeout
    manager: JobManager = request.app.state.manager
    row, outcome = manager.submit(
        project_id=version["project_id"],
        version_id=version_id,
        kind="solve",
        params=params,
        dedup_key=_radius_dedup(version_id, params, "solve"),
    )
    return JSONResponse_status_202(row, outcome)


@app.post("/api/versions/{version_id}/solve-incremental", status_code=202)
async def submit_incremental_solve(version_id: str, request: Request):
    payload = await _json(request)
    store: Store = request.app.state.store
    version = store.get_version(version_id)
    services.validate_solve_params(payload, {c["id"] for c in version["candidates"]})
    params = {
        "radius": float(payload["radius"]),
        "forced_site_ids": payload.get("forced_site_ids", []) or [],
    }
    if payload.get("timeout") is not None:
        params["timeout"] = float(payload["timeout"])
    manager: JobManager = request.app.state.manager
    row, outcome = manager.submit(
        project_id=version["project_id"],
        version_id=version_id,
        kind="incremental",
        params=params,
        dedup_key=_radius_dedup(version_id, params, "incremental"),
    )
    return JSONResponse_status_202(row, outcome)


def JSONResponse_status_202(row: dict, outcome: str) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={
            "job_id": row["id"],
            "status": row["status"],
            # created | active | reused | rerun
            "submit_outcome": outcome,
            # back-compatible flag: true when no new job record was made
            "deduplicated": outcome in ("active", "reused"),
        },
    )


@app.post("/api/versions/{version_id}/sweep", status_code=202)
async def submit_sweep(version_id: str, request: Request):
    payload = await _json(request)
    store: Store = request.app.state.store
    version = store.get_version(version_id)

    errors = []
    radii = payload.get("radii")
    if not isinstance(radii, list) or not radii:
        errors.append({"field": "radii", "message": "radii must be a non-empty list"})
    else:
        bad = [r for r in radii if isinstance(r, bool) or not isinstance(r, (int, float)) or r <= 0]
        if bad:
            errors.append({"field": "radii", "message": "every radius must be a positive number"})
    forced = payload.get("forced_site_ids", []) or []
    if not isinstance(forced, list) or not all(isinstance(f, str) for f in forced):
        errors.append({"field": "forced_site_ids", "message": "must be a list of site ids"})
    else:
        missing = [f for f in forced if f not in {c["id"] for c in version["candidates"]}]
        if missing:
            errors.append({"field": "forced_site_ids", "message": f"unknown site id(s): {', '.join(missing)}"})
    timeout = payload.get("timeout")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
    ):
        errors.append({"field": "timeout", "message": "timeout must be a positive number when given"})
    if errors:
        raise ValidationError(errors)

    params = {"radii": sorted(float(r) for r in radii), "forced_site_ids": list(forced)}
    if timeout is not None:
        params["timeout"] = float(timeout)
    manager: JobManager = request.app.state.manager
    row, outcome = manager.submit(
        project_id=version["project_id"],
        version_id=version_id,
        kind="sweep",
        params=params,
        # sweep jobs are not deduplicated (each scan is a fresh run)
        dedup_key=None,
    )
    return JSONResponse_status_202(row, outcome)


# ------------------------------------------------------------------- jobs --


def _serialize_job(j: dict) -> dict:
    return {
        "id": j["id"],
        "project_id": j["project_id"],
        "version_id": j["version_id"],
        "kind": j["kind"],
        "status": j["status"],
        "params": j["params"],
        "progress": j["progress"],
        "result": j["result"],
        "error": j["error"],
        "created_at": j["created_at"],
        "updated_at": j["updated_at"],
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    return _serialize_job(request.app.state.store.get_job(job_id))


@app.get("/api/versions/{version_id}/jobs")
def list_jobs(version_id: str, request: Request):
    store: Store = request.app.state.store
    store.get_version(version_id)
    return [_serialize_job(j) for j in store.list_jobs(version_id)]


@app.post("/api/jobs/{job_id}/cancel", status_code=200)
def cancel_job(job_id: str, request: Request):
    manager: JobManager = request.app.state.manager
    return _serialize_job(manager.cancel(job_id))
