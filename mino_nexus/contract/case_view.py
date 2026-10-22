"""用例行的源视图与编译就绪判定。

只做"这份文本/产物是否还对得上"的纯比较，不读写库——因此资源域也能用。
"""
from __future__ import annotations

import copy

from mino_nexus.core.stable_digest import digest

SOURCE_FIELDS = ("name", "platform", "precondition", "steps", "expected", "steps_raw", "expected_raw")


def source_view(case) -> dict:
    return {key: copy.deepcopy(case.get(key)) for key in SOURCE_FIELDS}


def readiness(case):
    if not case.get("source_revision_id"):
        return None  # Historical records remain readable; no invented provenance.
    if case.get("source_digest") != digest(source_view(case)):
        return "compile_source_changed"
    job, artifact = case.get("compile_job") or {}, case.get("effective_artifact") or {}
    if (job.get("status") != "succeeded" or job.get("job_id") != artifact.get("job_id")
            or artifact.get("revision_id") != case.get("source_revision_id")
            or job.get("job_id") != case.get("latest_compile_job_id")):
        return "compile_artifact_unavailable" if job.get("status") == "succeeded" else "compile_" + str(job.get("status") or "required")
    from mino_nexus.contract.build_identity import build_identity

    current = build_identity()
    if any((job.get("build") or {}).get(key) != current[key]
            for key in ("compiler_contract", "resource_catalog_version")):
        return "compile_contract_changed"
    return None
