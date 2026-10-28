"""页面入口档案 API。

按产品要求：**没有审核、发布、导入导出**。抽取完直接 active；这里只提供读取、
抽取和指纹试算。不改 `nav_fsm*` 的发布合同。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mino_nexus.api.deps import app_maintainer, current_session
from mino_nexus.core.http_util import ok
from mino_nexus.navigation.entries import extract as entry_extract
from mino_nexus.navigation.entries import store as entry_store
from mino_nexus.navigation.entries.fingerprint import page_fingerprint

router = APIRouter(prefix="/nav-fsm", tags=["NavPageEntries"])


class ExtractEntriesBody(BaseModel):
    project_id: str = ""
    platform: str = ""
    case_ids: list[str] = Field(default_factory=list)
    include_cases: bool = True
    include_capture: bool = True


class FingerprintBody(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    platform: str = ""


def _project_of(app_id: str, given: str = "") -> str:
    if str(given or "").strip():
        return str(given).strip()
    from mino_nexus.resources import project_store as ps

    app = ps.find_app(app_id)
    return str((app or {}).get("project_id") or "")


@router.get("/{app_id}/entries/summary")
def get_entries_summary(app_id: str, _sess: dict = Depends(current_session)):
    return ok(entry_store.summary(app_id))


@router.get("/{app_id}/entries/gaps")
def get_entry_gaps(
    app_id: str,
    status: str = "open",
    limit: int = 200,
    _sess: dict = Depends(current_session),
):
    return ok({
        "app_id": app_id,
        "status": status,
        "items": entry_store.list_gaps(app_id, status=status, limit=limit),
    })


@router.get("/{app_id}/entries")
def get_entries(
    app_id: str,
    target: str = "",
    from_state_id: str = "",
    status: str = "",
    platform: str = "",
    limit: int = 200,
    _sess: dict = Depends(current_session),
):
    return ok({
        "app_id": app_id,
        "items": entry_store.list_entries(
            app_id,
            target=target,
            from_state_id=from_state_id,
            status=status,
            platform=platform,
            limit=limit,
        ),
    })


@router.get("/{app_id}/entries/{entry_id}")
def get_entry(app_id: str, entry_id: str, _sess: dict = Depends(current_session)):
    row = entry_store.get_entry(app_id, entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="入口不存在")
    return ok(row)


@router.post("/{app_id}/entries/extract", dependencies=[Depends(app_maintainer)])
def extract_entries(
    app_id: str,
    body: ExtractEntriesBody,
    sess: dict = Depends(current_session),
):
    given_raw = str(body.project_id or "").strip()
    project_id = _project_of(app_id, given_raw)
    if not project_id:
        raise HTTPException(status_code=400, detail="缺少项目归属，无法读取用例/采集")
    if given_raw:
        from mino_nexus.resources import project_store as ps
        app = ps.require_app(app_id)
        expected = str(app.get("project_id") or "")
        given = ps.find_project(given_raw)
        given_id = str((given or {}).get("id") or given_raw).strip()
        if given_id != expected:
            raise HTTPException(status_code=422, detail={"reason_code": "app_project_mismatch", "message": "应用不属于该项目"})
        project_id = expected
    try:
        result = entry_extract.extract_for_app(
            app_id=app_id,
            project_id=project_id,
            platform=str(body.platform or ""),
            case_ids=list(body.case_ids or []) or None,
            include_cases=bool(body.include_cases),
            include_capture=bool(body.include_capture),
            actor=str(sess.get("user_id") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"入口抽取失败：{type(exc).__name__}: {exc}") from exc
    return ok(result)


@router.post("/{app_id}/entries/fingerprint", dependencies=[Depends(app_maintainer)])
def compute_fingerprint(app_id: str, body: FingerprintBody, _sess: dict = Depends(current_session)):
    """只做 DOM 结构指纹试算，不落库；用于入口详情/排查。"""
    del app_id
    fp = page_fingerprint(list(body.nodes or []), platform=str(body.platform or ""))
    return ok(fp)
