"""测试资源 MCP 读模型、预约/释放与统一取码。

账号列表/详情/预约返回明文账号密码；环境 secret 值和设备 session_key 仍不
直接回传。取码路由聚合账号固定码、环境固定码、Gmail 和人工兜底。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mino_nexus.api.deps import current_session, project_maintainer
from mino_nexus.core.http_util import ok
from mino_nexus.resources import resource_view

router = APIRouter(prefix="/project/{project_id}/resources", tags=["Resources"])


class ReserveAccountBody(BaseModel):
    account_id: str = ""
    env: str = ""
    lifecycle: str = ""
    run_id: str = ""
    ttl_sec: Optional[int] = Field(default=None, ge=60, le=7 * 24 * 3600)


class ReleaseAccountBody(BaseModel):
    run_id: str = Field(..., min_length=1)


class AccountOtpBody(BaseModel):
    env_profile: str = ""
    env_surface: str = ""
    node_id: str = ""
    sn: str = ""
    since_ts: Optional[float] = Field(default=None, ge=0)
    lookback_seconds: int = Field(default=600, ge=10, le=86400)
    max_wait_ms: int = Field(default=15000, ge=1000, le=45000)
    poll_interval_ms: int = Field(default=200, ge=200, le=10000)
    from_allowlist: list[str] = Field(default_factory=list, max_length=20)
    subject_contains: str = Field(default="", max_length=200)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"reason_code": "project_not_found", "message": "项目不存在"})


@router.get("/overview")
def resource_overview(
    project_id: str,
    env: str = "",
    _sess: dict = Depends(current_session),
):
    try:
        return ok(resource_view.overview(project_id, env=env))
    except KeyError as exc:
        raise _not_found() from exc


@router.get("/accounts")
def resource_accounts(
    project_id: str,
    env: str = "",
    state: str = "",
    lifecycle: str = "",
    q: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _sess: dict = Depends(current_session),
):
    try:
        return ok(resource_view.accounts(
            project_id, env=env, state=state, lifecycle=lifecycle, q=q, limit=limit, offset=offset,
        ))
    except KeyError as exc:
        raise _not_found() from exc


@router.get("/accounts/{account_id}")
def resource_account_detail(
    project_id: str,
    account_id: str,
    _sess: dict = Depends(current_session),
):
    try:
        data = resource_view.account_detail(project_id, account_id)
    except KeyError as exc:
        raise _not_found() from exc
    if data is None:
        raise HTTPException(status_code=404, detail={"reason_code": "account_not_found", "message": "账号不存在"})
    return ok(data)


@router.post("/accounts/reserve", dependencies=[Depends(project_maintainer)])
def resource_account_reserve(
    project_id: str,
    body: ReserveAccountBody,
    _sess: dict = Depends(current_session),
):
    try:
        account, error, run_id = resource_view.reserve_account(
            project_id,
            account_id=body.account_id,
            env=body.env,
            lifecycle=body.lifecycle,
            run_id=body.run_id,
            ttl_sec=body.ttl_sec,
        )
    except KeyError as exc:
        raise _not_found() from exc
    if error:
        status = 409 if any(
            key in error for key in ("占用", "锁定", "状态发生", "不可用", "不满足", "已被", "读取失败")
        ) else 404
        raise HTTPException(status_code=status, detail={"reason_code": "reserve_failed", "message": error})
    return ok({"account": account, "run_id": run_id, "lease": (account or {}).get("lease") or {}})


@router.post("/accounts/{account_id}/otp", dependencies=[Depends(project_maintainer)])
def resource_account_otp(
    project_id: str,
    account_id: str,
    body: AccountOtpBody,
    sess: dict = Depends(current_session),
):
    """统一取码：账号固定码 -> 环境固定码 -> Gmail -> 人工。"""
    try:
        data = resource_view.account_otp(
            project_id, account_id,
            env_profile=body.env_profile, env_surface=body.env_surface,
            node_id=body.node_id, sn=body.sn,
            plugin_user_id=str(sess.get("user_id") or ""),
            from_allowlist=body.from_allowlist or None,
            subject_contains=body.subject_contains,
        )
    except KeyError as exc:
        raise _not_found() from exc
    if data is None:
        raise HTTPException(status_code=404, detail={"reason_code": "account_not_found", "message": "账号不存在"})
    if data.get("source") != "gmail":
        summary = "已取到验证码" if data.get("code") else (
            "需要人工输入验证码" if data.get("requires_human") else "未取到验证码"
        )
        return ok(data, msg=summary)
    from mino_nexus.api.rRuntime import GmailOtpBody, fetch_gmail_otp

    gmail = data.get("gmail") if isinstance(data.get("gmail"), dict) else {}
    try:
        res = fetch_gmail_otp(
            GmailOtpBody(
                node_id=body.node_id,
                to_address=str(data.get("recipient") or ""),
                since_ts=body.since_ts,
                lookback_seconds=body.lookback_seconds,
                max_wait_ms=body.max_wait_ms,
                poll_interval_ms=body.poll_interval_ms,
                subject_contains=body.subject_contains or str(gmail.get("subject_contains") or ""),
                from_allowlist=body.from_allowlist or list(gmail.get("from_allowlist") or []),
            ),
            sess,
        )
    except HTTPException as exc:
        raise HTTPException(
            status_code=409 if exc.status_code in (409, 422) else 502,
            detail={"reason_code": "otp_gmail_failed", "message": str(exc.detail), "source": "gmail"},
        ) from exc
    code = str((res.get("data") or {}).get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=502, detail={"reason_code": "otp_gmail_empty", "message": "Gmail 未返回验证码", "source": "gmail"})
    data.update(code=code, source="gmail", requires_human=False, hint="已从 Gmail 取到验证码")
    return ok(data, msg="已取到 Gmail 验证码")


@router.post("/accounts/{account_id}/release", dependencies=[Depends(project_maintainer)])
def resource_account_release(
    project_id: str,
    account_id: str,
    body: ReleaseAccountBody,
    _sess: dict = Depends(current_session),
):
    try:
        released, error = resource_view.release_account(project_id, account_id, run_id=body.run_id)
    except KeyError as exc:
        raise _not_found() from exc
    if not released:
        if error == "账号不存在":
            raise HTTPException(status_code=404, detail={"reason_code": "account_not_found", "message": error})
        raise HTTPException(status_code=409, detail={"reason_code": "release_failed", "message": error})
    return ok({"account_id": account_id, "released": True})


@router.get("/devices")
def resource_devices(
    project_id: str,
    status: str = "",
    platform: str = "",
    sn: str = "",
    include_sessions: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _sess: dict = Depends(current_session),
):
    try:
        return ok(resource_view.devices(
            project_id, status=status, platform=platform, sn=sn,
            include_sessions=include_sessions, limit=limit, offset=offset,
        ))
    except KeyError as exc:
        raise _not_found() from exc


@router.get("/logs")
def resource_logs(
    project_id: str,
    action: str = "",
    env: str = "",
    run_id: str = "",
    case_id: str = "",
    account_ident: str = "",
    sn: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    _sess: dict = Depends(current_session),
):
    try:
        return ok(resource_view.logs(
            project_id, action=action, env=env, run_id=run_id, case_id=case_id,
            account_ident=account_ident, sn=sn, page=page, page_size=page_size,
        ))
    except KeyError as exc:
        raise _not_found() from exc


@router.get("/script-guide")
def script_guide(project_id: str, _sess: dict = Depends(current_session)):
    """仓库位置和 case 基础指令。给要写用例脚本的工具看，不含仓库凭据。"""
    from mino_nexus.casework.script_workspace import ensure_workspace, get_repo, public_url, venv_python
    from mino_nexus.loop.script_api import script_contract
    from mino_nexus.resources import project_store as ps

    try:
        ps.require_project(project_id)
    except KeyError as exc:
        raise _not_found() from exc
    repo = get_repo(project_id)
    place = ensure_workspace(project_id)
    return ok({
        "workspace": place,
        "repo": {
            "configured": bool(str(repo.get("url") or "").strip()),
            "url": public_url(repo.get("url") or ""),
            "branch": repo.get("branch") or "",
            "local_checkout": place["checkout"],
            "venv_python": str(venv_python(project_id)),
        },
        "layout": {
            "one_repo_per_project": True,
            "edit_local_files": True,
            "path_field": "script_path",
            "path_rule": "用例记录仓库内的相对路径，必须是 .py，且不能写出仓库。一个 case_id 只有 tests/<需求ID>/<case_id>.py，不按渠道拆文件；目录一律用需求ID（req-xxx），不用中文、不用 module 名。",
            "save": "在 workspace.checkout 里直接改文件。不要把脚本文本交给 Nexus 保存。",
            "scripts_package": "scripts/",
            "tests_root": "tests/<需求ID>/<case_id>.py",
            "pages_dir": "pages/<page_key>.yaml",
        },
        **script_contract(),
    })


@router.get("/env")
def resource_env(
    project_id: str,
    _sess: dict = Depends(current_session),
):
    try:
        return ok(resource_view.environment(project_id))
    except KeyError as exc:
        raise _not_found() from exc
