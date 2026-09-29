"""登录态来源：工具接口 > 能证明的当前屏 > 测试资源里已记录的设备会话。"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

_WEB_PLATFORMS = frozenset({"web", "browser", "playwright"})
_AUTH_PROBE_STEP = -7
_OPEN_BROWSERS: set[str] = set()


def _task_browser_key(ctx: Any) -> str:
    rid = str(getattr(ctx, "run_id", "") or "")
    task = rid.split("::", 1)[0] if "::" in rid else rid
    sn, pkg = _sn_pkg(ctx)
    return f"{task}|{sn}|{pkg}"


def note_web_context_start(ctx: Any) -> None:
    """同一任务里第一次打开网页记为未登录。之后同进程里的打开沿用已记录的登录态。"""
    if _platform(ctx) not in _WEB_PLATFORMS:
        return
    key = _task_browser_key(ctx)
    if not key.strip("|") or key in _OPEN_BROWSERS:
        return
    _OPEN_BROWSERS.add(key)
    from mino_nexus.services.resource_transition import emit_device_logout

    emit_device_logout(ctx, source="browser_start")


def note_web_context_closed(ctx: Any) -> None:
    if _platform(ctx) not in _WEB_PLATFORMS:
        return
    _OPEN_BROWSERS.discard(_task_browser_key(ctx))


def _platform(ctx: Any) -> str:
    return str(getattr(ctx, "platform", "") or "").strip().lower()


def _sn_pkg(ctx: Any) -> tuple[str, str]:
    sn = str(getattr(ctx, "sn", "") or "").strip()
    pkg = str(getattr(ctx, "target_package", "") or "").strip()
    return sn, pkg


def remember_session(ctx: Any, session: str, source: str) -> None:
    """写入本趟上下文，并在有设备标识时落到 device_app_sessions。"""
    sess = str(session or "unknown").strip().lower() or "unknown"
    src = str(source or "")[:32]
    setattr(ctx, "device_session", sess)
    setattr(ctx, "device_session_source", src)
    scene = getattr(ctx, "case_scene", None)
    if isinstance(scene, dict):
        scene["observed_session"] = sess
        scene["observed_session_source"] = src
    sn, pkg = _sn_pkg(ctx)
    if not sn or not pkg or sess == "unknown":
        return
    if sess == "logged_in":
        from mino_nexus.services.resource_transition import emit_login_complete

        emit_login_complete(ctx, package_id=pkg)
        return
    if sess == "logged_out":
        from mino_nexus.services.resource_transition import emit_device_logout

        emit_device_logout(ctx, package_id=pkg, source=src or "probe", sync_account=True)
        return
    from mino_nexus.services.device_app_session_store import upsert_session

    upsert_session(
        sn,
        pkg,
        session=sess,
        stale=False,
        lease_run_id=str(getattr(ctx, "run_id", "") or "")[:80],
        source=src or "probe",
    )


def stored_session(ctx: Any) -> tuple[str, str]:
    """返回 (session, note)。跨任务记录只作提示，不覆盖本趟已写入的值。"""
    sn, pkg = _sn_pkg(ctx)
    if not sn or not pkg:
        return "unknown", "无设备会话记录"
    from mino_nexus.services.device_app_session_store import get_session

    row = get_session(sn, pkg) or {}
    sess = str(row.get("session") or "unknown").strip().lower() or "unknown"
    run_id = str(getattr(ctx, "run_id", "") or "").strip()
    same = bool(run_id) and str(row.get("lease_run_id") or "") == run_id
    if sess == "unknown":
        return "unknown", "测试资源中登录态未知"
    note = "本趟已记录的登录态" if same else "跨任务登录态提示，执行中若不一致再登录或登出"
    return sess, note


def screen_proves_session(row: dict[str, Any]) -> bool:
    """隐私协议、启动弹窗不能当成登录态。登录入口或已登录主页可以。"""
    if not row or not row.get("ok"):
        return False
    session = str(row.get("session") or "").strip().lower()
    text = f"{row.get('reason') or ''} {row.get('seen') or ''}"
    if any(w in text for w in ("协议", "隐私", "同意并继续", "同意", "启动弹窗")):
        return False
    if session == "logged_in" and any(
        w in text for w in ("已登录", "退出登录", "个人主页", "Log out", "Sign out")
    ):
        return True
    if session in ("guest", "logged_out") and any(
        w in text for w in ("登录按钮", "登录页", "登录入口", "未登录", "Log in", "Sign in")
    ):
        return True
    return False


def _is_web_ctx(ctx: Any) -> bool:
    from mino_nexus.runtime.run_context import is_web_slot

    return is_web_slot(str(getattr(ctx, "sn", "") or ""), _platform(ctx))


def _host_of(url: str) -> str:
    return (urlparse(str(url or "")).hostname or "").lower().rstrip(".")


def _hosts_related(left: str, right: str) -> bool:
    a = str(left or "").lower().rstrip(".")
    b = str(right or "").lower().rstrip(".")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _app_web_url(ctx: Any) -> str:
    for attr in ("page_url", "target_package"):
        val = str(getattr(ctx, attr, "") or "").strip()
        if val.startswith("http://") or val.startswith("https://"):
            return val
    return ""


def probe_web_auth(ctx: Any, router: Any) -> dict[str, Any] | None:
    """网页 Cookie / localStorage / sessionStorage。没有页面或能力未接通时返回 None。"""
    if router is None or not _is_web_ctx(ctx):
        return None
    from mino_nexus.core.protocol import EventStatus
    from mino_nexus.core.schemas import PlanEvent

    app_url = _app_web_url(ctx)
    event = PlanEvent(
        seq=_AUTH_PROBE_STEP,
        capability_id="read_web_auth",
        params={"url": app_url} if app_url else {},
        label="读取网页登录存储",
        expected_executor="playwright",
    )
    run_id = str(getattr(ctx, "scout_run_id", "") or getattr(ctx, "run_id", "") or "")
    from mino_nexus.loop.program_tool_log import log_program_tool
    from mino_nexus.loop.session_log import active_writer

    writer = active_writer()
    try:
        res = router.dispatch(event, run_id=run_id, step_idx=_AUTH_PROBE_STEP, ctx=ctx)
    except Exception as exc:
        summary = f"read_web_auth 调用失败：{exc}"[:240]
        log_program_tool(
            writer,
            capability_id="read_web_auth",
            params={"url": app_url} if app_url else {},
            status="fail",
            summary=summary,
            executor_used="playwright",
            source="relogin",
        )
        return {"session": "unknown", "source": "tool_api", "summary": summary, "failed": True}
    status = getattr(res, "status", None)
    status_val = status.value if hasattr(status, "value") else str(status or "")
    raw = str(getattr(res, "summary", "") or "").strip()
    log_program_tool(
        writer,
        capability_id="read_web_auth",
        params={"url": app_url} if app_url else {},
        status=status_val or "fail",
        summary=raw[:240] or "read_web_auth 无结果",
        executor_used=str(getattr(res, "executor_used", "") or "playwright"),
        source="relogin",
    )
    if status != EventStatus.PASS:
        return {
            "session": "unknown",
            "source": "tool_api",
            "summary": raw[:240] or "read_web_auth 未通过",
            "failed": True,
        }
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "session": "unknown",
            "source": "tool_api",
            "summary": "read_web_auth 返回的不是登录态",
            "failed": True,
        }
    if not isinstance(data, dict):
        return {
            "session": "unknown",
            "source": "tool_api",
            "summary": "read_web_auth 返回的不是登录态",
            "failed": True,
        }
    session = str(data.get("session") or "unknown").strip().lower()
    evidence = str(data.get("evidence") or "").strip()
    host = str(data.get("host") or "").strip().lower()
    expected = _host_of(app_url)
    if session == "logged_in" and (
        evidence not in ("jwt", "auth_token")
        or not host
        or (expected and not _hosts_related(host, expected))
    ):
        return {
            "session": "unknown",
            "source": "tool_api",
            "summary": "凭据没有落在当前被测网页上，不能当成已登录",
            "failed": True,
        }
    if session not in ("logged_in", "logged_out", "guest"):
        return {
            "session": "unknown",
            "source": "tool_api",
            "summary": raw[:240] or "网页登录存储未能确认登录态",
            "failed": True,
        }
    return {"session": session, "source": "tool_api", "summary": raw[:240]}


def ensure_read_web_auth_capability() -> int:
    """目录里登记网页登录存储读取。visible_to=system，不进模型菜单。"""
    from mino_nexus.catalog import registry as catalog
    from mino_nexus.catalog.writer import upsert

    if catalog.get_capability("read_web_auth") is not None:
        return 0
    upsert(
        "generic",
        "read_web_auth",
        {
            "display_name": "读取网页登录存储",
            "description": "程序读取 Cookie 与本地存储是否含登录凭据。",
            "platforms": ["web"],
            "visible_to": ["system"],
            "payload": {
                "params": [],
                "implementations": [{"id": "playwright", "executor": "playwright"}],
            },
        },
    )
    return 1


def observe_login_state(ctx: Any, router: Any, shot: Any) -> tuple[str, str]:
    """按优先级给出本趟登录态，并写入上下文。网页只读登录存储，不看图。"""
    if _is_web_ctx(ctx):
        api = probe_web_auth(ctx, router) or {}
        session = str(api.get("session") or "unknown").strip().lower()
        if session in ("logged_in", "logged_out", "guest") and not api.get("failed"):
            remember_session(ctx, session, "tool_api")
            return session, f"接口确认 session={session}"
        stored, note = stored_session(ctx)
        if stored != "unknown":
            setattr(ctx, "device_session", stored)
            setattr(ctx, "device_session_source", "resource")
            scene = getattr(ctx, "case_scene", None)
            if isinstance(scene, dict):
                scene["observed_session"] = stored
                scene["observed_session_source"] = "resource"
            return stored, f"{note}：session={stored}"
        setattr(ctx, "device_session", "unknown")
        setattr(ctx, "device_session_source", "tool_api")
        reason = str(api.get("summary") or "").strip() or "网页登录存储未能确认登录态"
        return "unknown", reason

    from mino_nexus.ai.planner import inspect_session
    from mino_nexus.runtime.session_gate import format_required_session_brief

    image = ""
    mime = "image/png"
    if shot is not None and getattr(shot, "has_image", lambda: False)():
        image = shot.image_base64
        mime = getattr(shot, "image_mime", None) or "image/png"
    scene = getattr(ctx, "case_scene", None)
    row = inspect_session(
        required_session=format_required_session_brief(scene),
        accounts_brief=str(getattr(ctx, "accounts_brief", "") or ""),
        image_base64=image,
        image_mime=mime,
        screen_w=int(getattr(shot, "width", 0) or 0) if shot else 0,
        screen_h=int(getattr(shot, "height", 0) or 0) if shot else 0,
    )
    if screen_proves_session(row):
        session = str(row.get("session") or "unknown").strip().lower()
        if session == "guest":
            session = "logged_out"
        stored, _stored_note = stored_session(ctx)
        remember_session(ctx, session, "screen")
        reason = str(row.get("reason") or "").strip() or f"当前屏证明 session={session}"
        if stored not in ("", "unknown") and stored != session:
            setattr(ctx, "session_contradiction", {"stored": stored, "screen": session})
            reason = f"同任务登录态矛盾：已记录 {stored}，当前屏 {session}。{reason}"
        return session, reason

    stored, note = stored_session(ctx)
    if stored != "unknown":
        setattr(ctx, "device_session", stored)
        setattr(ctx, "device_session_source", "resource")
        if isinstance(scene, dict):
            scene["observed_session"] = stored
            scene["observed_session_source"] = "resource"
        return stored, f"{note}：session={stored}"
    setattr(ctx, "device_session", "unknown")
    setattr(ctx, "device_session_source", "unknown")
    return "unknown", "接口与当前屏都无法证明登录态，测试资源中也没有记录"
