"""Capture actual compile-time calls without exporting prompts or inputs."""
from contextvars import ContextVar
from functools import wraps

_records = ContextVar("case_compile_provenance", default=None)


def record(meta, job_meta):
    records = _records.get()
    if records is not None:
        records.append({"dispatch_id": (meta or {}).get("dispatch_id"), "job_id": job_meta.get("job_id"),
            "prompt_version": job_meta.get("prompt_version"), "prompt_sha": job_meta.get("prompt_sha")})


def capture(fn):
    @wraps(fn)
    def wrapped(case):
        records = []
        token = _records.set(records)
        try:
            result = fn(case)
            inherited = (case.get("meta") or {}).get("compile_provenance") or []
            result["compile_provenance"] = list(inherited) + records
            return result
        finally:
            _records.reset(token)
    return wrapped
