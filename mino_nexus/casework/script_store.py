"""用例脚本与项目级前置/后置的读写。真源表 case_scripts。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from mino_nexus.casework.script_check import check_script
from mino_nexus.core.platform_contract import TARGET_PLATFORMS

APP_ENDS = TARGET_PLATFORMS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(*parts: str) -> str:
    blob = hashlib.sha256()
    for part in parts:
        blob.update(str(part or "").encode())
        blob.update(b"\0")
    return blob.hexdigest()


def _empty() -> dict[str, str]:
    return {"run": "", "pretest": "", "posttest": "", "digest": "", "updated_at": ""}


def _public(row) -> dict[str, str]:
    if row is None:
        return _empty()
    return {
        "run": str(row.run_body or ""),
        "pretest": str(row.pretest_body or ""),
        "posttest": str(row.posttest_body or ""),
        "digest": str(row.digest or ""),
        "updated_at": str(row.updated_at or ""),
    }


def get_scripts(project_id: str, case_id: str = "") -> dict[str, str]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case_script import CaseScript

    pid = str(project_id or "").strip()
    cid = str(case_id or "").strip()
    if not pid:
        return _empty()
    with session_scope() as db:
        row = db.query(CaseScript).filter(
            CaseScript.project_id == pid, CaseScript.case_id == cid,
        ).one_or_none()
        return _public(row)


def _end(platform: str) -> str:
    plat = str(platform or "").strip().lower()
    if plat not in APP_ENDS:
        raise ValueError("script_platform_invalid")
    return plat


def _has_body(row: dict[str, str]) -> bool:
    return any(str(row.get(key) or "").strip() for key in ("run", "pretest", "posttest"))


def get_platform_scripts(project_id: str, case_id: str, platform: str) -> dict[str, str]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case_platform_script import CasePlatformScript

    pid = str(project_id or "").strip()
    cid = str(case_id or "").strip()
    plat = _end(platform)
    if not pid:
        return _empty()
    with session_scope() as db:
        row = db.query(CasePlatformScript).filter(
            CasePlatformScript.project_id == pid,
            CasePlatformScript.case_id == cid,
            CasePlatformScript.platform == plat,
        ).one_or_none()
        return _public(row)


def scripts_by_platform(project_id: str, case_id: str = "") -> dict[str, dict[str, str]]:
    return {plat: get_platform_scripts(project_id, case_id, plat) for plat in APP_ENDS}


def load_bundle(project_id: str, case_id: str, platform: str = "") -> dict[str, str]:
    """执行时取出这一端的脚本。该端没有正文时，用未分端的那一份。"""
    legacy_case = get_scripts(project_id, case_id)
    legacy_project = get_scripts(project_id, "")
    plat = str(platform or "").strip().lower()
    if plat in APP_ENDS:
        scoped_case = get_platform_scripts(project_id, case_id, plat)
        scoped_project = get_platform_scripts(project_id, "", plat)
        case = scoped_case if _has_body(scoped_case) else legacy_case
        project = scoped_project if _has_body(scoped_project) else legacy_project
    else:
        case, project = legacy_case, legacy_project
    return {
        "run": case["run"],
        "case_pretest": case["pretest"],
        "case_posttest": case["posttest"],
        "project_pretest": project["pretest"],
        "project_posttest": project["posttest"],
        "digest": case["digest"],
        "project_digest": project["digest"],
    }


def save_scripts(
    project_id: str,
    case_id: str,
    *,
    run: str | None = None,
    pretest: str | None = None,
    posttest: str | None = None,
) -> dict[str, str]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case_script import CaseScript

    pid = str(project_id or "").strip()
    cid = str(case_id or "").strip()
    if not pid:
        raise ValueError("缺少 project_id")
    if cid == "" and run is not None and str(run).strip():
        raise ValueError("项目脚本只有 pretest 和 posttest")
    current = get_scripts(pid, cid)
    fields = {
        "run": current["run"] if run is None else str(run),
        "pretest": current["pretest"] if pretest is None else str(pretest),
        "posttest": current["posttest"] if posttest is None else str(posttest),
    }
    if run is not None:
        check_script(fields["run"], role="run")
    if pretest is not None:
        check_script(fields["pretest"], role="pretest")
    if posttest is not None:
        check_script(fields["posttest"], role="posttest")
    digest = _digest(fields["run"], fields["pretest"], fields["posttest"])
    updated = _now()
    with session_scope() as db:
        row = db.query(CaseScript).filter(
            CaseScript.project_id == pid, CaseScript.case_id == cid,
        ).one_or_none()
        if row is None:
            row = CaseScript(project_id=pid, case_id=cid)
            db.add(row)
        row.run_body = fields["run"]
        row.pretest_body = fields["pretest"]
        row.posttest_body = fields["posttest"]
        row.digest = digest
        row.updated_at = updated
        db.flush()
    return {**fields, "digest": digest, "updated_at": updated}


def save_platform_scripts(
    project_id: str,
    case_id: str,
    platform: str,
    *,
    run: str | None = None,
    pretest: str | None = None,
    posttest: str | None = None,
) -> dict[str, str]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case_platform_script import CasePlatformScript

    pid = str(project_id or "").strip()
    cid = str(case_id or "").strip()
    plat = _end(platform)
    if not pid:
        raise ValueError("缺少 project_id")
    if cid == "" and run is not None and str(run).strip():
        raise ValueError("项目脚本只有 pretest 和 posttest")
    current = get_platform_scripts(pid, cid, plat)
    fields = {
        "run": current["run"] if run is None else str(run),
        "pretest": current["pretest"] if pretest is None else str(pretest),
        "posttest": current["posttest"] if posttest is None else str(posttest),
    }
    if run is not None:
        check_script(fields["run"], role="run")
    if pretest is not None:
        check_script(fields["pretest"], role="pretest")
    if posttest is not None:
        check_script(fields["posttest"], role="posttest")
    digest = _digest(plat, fields["run"], fields["pretest"], fields["posttest"])
    updated = _now()
    with session_scope() as db:
        row = db.query(CasePlatformScript).filter(
            CasePlatformScript.project_id == pid,
            CasePlatformScript.case_id == cid,
            CasePlatformScript.platform == plat,
        ).one_or_none()
        if row is None:
            row = CasePlatformScript(project_id=pid, case_id=cid, platform=plat)
            db.add(row)
        row.run_body = fields["run"]
        row.pretest_body = fields["pretest"]
        row.posttest_body = fields["posttest"]
        row.digest = digest
        row.updated_at = updated
        db.flush()
    return {**fields, "platform": plat, "digest": digest, "updated_at": updated}


def attach(case: dict[str, Any]) -> dict[str, Any]:
    """把脚本附到用例读模型上，供详情接口返回。"""
    pid = str(case.get("project_id") or "")
    cid = str(case.get("case_id") or "")
    scripts = get_scripts(pid, cid)
    by_end = {
        plat: {
            "script": row["run"],
            "pretest": row["pretest"],
            "posttest": row["posttest"],
        }
        for plat, row in scripts_by_platform(pid, cid).items()
    }
    return {
        **case,
        "script": scripts["run"],
        "pretest": scripts["pretest"],
        "posttest": scripts["posttest"],
        "scripts_by_platform": by_end,
    }
