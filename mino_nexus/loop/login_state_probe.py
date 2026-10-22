"""当前屏正向证据确认登录；凭据与旧登记仅作线索。"""
from __future__ import annotations

from mino_nexus.navigation.login.transition import update_auth_state

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
    """新浏览器作用域标记待观察；打开本身不能证明未登录。"""
    if _platform(ctx) not in _WEB_PLATFORMS:
        return
    key = _task_browser_key(ctx)
    if not key.strip("|") or key in _OPEN_BROWSERS:
        return
    _OPEN_BROWSERS.add(key)
    from mino_nexus.casework.resource_transition import mark_web_run_devices

    sn, pkg = _sn_pkg(ctx)
    if not sn or not pkg:
        return
    mark_web_run_devices(
        str(getattr(ctx, "run_id", "") or ""),
        "unknown",
        source="browser_start",
        package=pkg,
        doc={"sn": sn, "package": pkg, "sns": [sn], "platform": "web"},
    )


def note_web_context_closed(ctx: Any) -> None:
    if _platform(ctx) not in _WEB_PLATFORMS:
        return
    _OPEN_BROWSERS.discard(_task_browser_key(ctx))
    from mino_nexus.loop.observe.session_persist import mark_session_dirty
    mark_session_dirty(ctx, reason="browser_context_closed")


def _platform(ctx: Any) -> str:
    return str(getattr(ctx, "platform", "") or "").strip().lower()


def _sn_pkg(ctx: Any) -> tuple[str, str]:
    sn = str(getattr(ctx, "sn", "") or "").strip()
    pkg = str(getattr(ctx, "target_package", "") or "").strip()
    return sn, pkg


def remember_session(ctx: Any, session: str, source: str) -> None:
    """写入本趟上下文，并在有设备标识时落到 device_app_sessions。"""
    from mino_nexus.resources.device_app_session_store import upsert_session
    from mino_nexus.resources.accounts.session_match import normalize_device_session

    sess = normalize_device_session(session)
    if sess == "logged_in":
        from mino_nexus.navigation.login.evidence import can_commit
        if not can_commit(ctx):
            sess = "unknown"
    if sess != "logged_in":
        update_auth_state(ctx, login_evidence={})
    src = str(source or "")[:32]
    if src in {"tool_api", "screen", "flow_block", "inspect_session", "logout"}:
        update_auth_state(ctx, session_fact={"session": sess, "source": src})
        ctx.session_dirty = sess not in {"logged_in", "logged_out", "guest"}
    update_auth_state(ctx, device_session=sess)
    update_auth_state(ctx, device_session_source=src)
    scene = getattr(ctx, "case_scene", None)
    if isinstance(scene, dict):
        scene["observed_session"] = sess
        scene["observed_session_source"] = src
    sn, pkg = _sn_pkg(ctx)
    if not sn or not pkg:
        return
    if sess == "logged_in":
        from mino_nexus.casework.resource_transition import emit_login_complete

        emit_login_complete(ctx, package_id=pkg)
        return
    if sess == "logged_out":
        from mino_nexus.casework.resource_transition import emit_device_logout

        emit_device_logout(ctx, package_id=pkg, source=src or "probe", sync_account=True)
        return
    if sess == "guest":
        from mino_nexus.resources.device_app_session_store import upsert_session

        upsert_session(
            sn,
            pkg,
            session="guest",
            clear_binding=True,
            stale=False,
            stale_reason="",
            lease_run_id=str(getattr(ctx, "run_id", "") or "")[:80],
            source=src or "probe",
        )
        return
    from mino_nexus.resources.device_app_session_store import upsert_session

    upsert_session(
        sn,
        pkg,
        session=sess,
        stale=sess not in {"logged_in", "logged_out", "guest"},
        lease_run_id=str(getattr(ctx, "run_id", "") or "")[:80],
        source=src or "probe",
    )


def stored_session(ctx: Any) -> tuple[str, str]:
    """返回 (session, note)。跨任务记录只作提示，不覆盖本趟已写入的值。"""
    sn, pkg = _sn_pkg(ctx)
    if not sn or not pkg:
        return "unknown", "没有当前设备会话证据，须观察确认"
    from mino_nexus.resources.device_app_session_store import get_session
    from mino_nexus.resources.accounts.session_match import normalize_device_session

    if bool(getattr(ctx, "session_dirty", False)) or _is_web_ctx(ctx):
        return "unknown", "当前作用域须重新观察，不能继承登记簿"
    row = get_session(sn, pkg) or {}
    sess = "unknown" if row.get("stale") else normalize_device_session(row.get("session") or "")
    run_id = str(getattr(ctx, "run_id", "") or "").strip()
    same = bool(run_id) and str(row.get("lease_run_id") or "") == run_id
    if not same:
        return "unknown", "跨任务登记簿只作提示，须当前界面确认"
    return sess, "本趟已记录的登录态"


def screen_proves_session(row: dict[str, Any]) -> bool:
    """只认结构化 session。不从模型说明文字里找「已登录」「登录页」。"""
    if not row or not row.get("ok"):
        return False
    session = str(row.get("session") or "").strip().lower()
    return session in {"logged_in", "logged_out", "guest"}


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


def _unknown_probe(summary: str) -> dict[str, Any]:
    return {"session": "unknown", "source": "tool_api", "summary": summary}


def _web_probe_session(data: dict[str, Any], app_url: str) -> str:
    """当前站点的令牌只证明已登录；读失败或没令牌须 UI 观察。"""
    evidence = str(data.get("evidence") or "").strip()
    host = str(data.get("host") or "").strip().lower()
    expected = _host_of(app_url)
    if str(data.get("session") or "").strip().lower() != "logged_in":
        return "unknown"
    if evidence not in ("jwt", "auth_token") or not host:
        return "unknown"
    if expected and not _hosts_related(host, expected):
        return "unknown"
    return "logged_in"


def probe_web_auth(ctx: Any, router: Any) -> dict[str, Any] | None:
    """网页登录存储。无可靠证据时返回 unknown，交给当前截图观察。"""
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
    from mino_nexus.loop.observe.program_tool_log import log_program_tool
    from mino_nexus.records.eventlog.writer import active_writer

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
        return _unknown_probe(summary)
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
        return _unknown_probe(raw[:240] or "read_web_auth 未通过，需要当前界面确认")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _unknown_probe("read_web_auth 返回的不是登录态，需要当前界面确认")
    if not isinstance(data, dict):
        return _unknown_probe("read_web_auth 返回的不是登录态，需要当前界面确认")
    session = _web_probe_session(data, app_url)
    if session == "logged_in":
        return {"session": "unknown", "source": "tool_api", "credential_present": True,
                "summary": "发现当前站点凭据，尚须当前界面的登录与身份核验"}
    return _unknown_probe("凭据没有落在当前被测网页上，需要当前界面确认")


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
                "caller": "system", "phases": [], "observe": "program",
                "params": [],
                "implementations": [{"id": "playwright", "executor": "playwright"}],
            },
        },
    )
    return 1


def _log_probe_diagnostic(ctx: Any, event_type: str, payload: dict[str, Any]) -> None:
    """Only attach probe diagnostics to the current case's session."""
    from mino_nexus.core.log import SLog
    from mino_nexus.records.eventlog.writer import active_writer

    writer = active_writer()
    sid = str(getattr(ctx, "session_id", "") or "").strip()
    if not sid or writer is None or writer.session_id != sid:
        SLog.w("LoginStateProbe", f"{event_type}: 缺少匹配的会话 writer，诊断未落盘")
        return
    writer.append(event_type, payload)


def observe_login_state(ctx: Any, router: Any, shot: Any) -> tuple[str, str]:
    """当前接口或界面证明登录态；缺少证据时保留 unknown。"""
    from mino_nexus.loop.observe.session_persist import stamp_session_observation
    ctx.web_auth_hint = None
    ctx.session_contradiction = None
    if _is_web_ctx(ctx):
        api = probe_web_auth(ctx, router) or {}
        # Storage is only a hint. Never return early or update account state.
        ctx.web_auth_hint = bool(api.get("credential_present"))
        _log_probe_diagnostic(ctx, "login/web_auth_hint", {
            "credential_present": ctx.web_auth_hint,
            "authentication_verified": False,
            "source": "web_storage",
        })

    from mino_nexus.ai.planner import inspect_session
    from mino_nexus.runtime.session_gate import format_required_session_brief

    if shot is None or not getattr(shot, "has_image", lambda: False)():
        from mino_nexus.loop.channel_observation import capture_decision_frame
        if router is not None:
            shot, _ = capture_decision_frame(router, ctx, hierarchy=False)
    image = ""
    mime = "image/png"
    if shot is not None and getattr(shot, "has_image", lambda: False)():
        image = shot.image_base64
        mime = getattr(shot, "image_mime", None) or "image/png"
    scene = getattr(ctx, "case_scene", None)
    row = inspect_session(
        run_context=ctx,
        required_session=format_required_session_brief(scene),
        knowledge_hint=str(getattr(ctx, "session_knowledge_hint", "") or ""),
        accounts_brief=str(getattr(ctx, "accounts_brief", "") or ""),
        image_base64=image,
        image_mime=mime,
        screen_w=int(getattr(shot, "width", 0) or 0) if shot else 0,
        screen_h=int(getattr(shot, "height", 0) or 0) if shot else 0,
    )
    if screen_proves_session(row):
        from mino_nexus.resources.accounts.session_match import normalize_device_session

        observed_session = normalize_device_session(row.get("session") or "")
        stored, _stored_note = stored_session(ctx)
        from mino_nexus.loop.observe.session_persist import stamp_session_observation
        effective = stamp_session_observation(ctx, row) or row
        session = str(effective.get("session") or "unknown")
        update_auth_state(ctx, device_session=session)
        update_auth_state(ctx, device_session_source="screen")
        reason = str(row.get("reason") or "").strip() or f"当前屏证明 session={session}"
        if stored in {"logged_in", "logged_out", "guest"} and stored != observed_session:
            ctx.session_contradiction = {"stored": stored, "screen": observed_session}
            _log_probe_diagnostic(ctx, "login/session_contradiction", {
                **ctx.session_contradiction,
                "effective_session": session,
                "source": "screen",
            })
            reason = f"同任务登录态矛盾：已记录 {stored}，当前屏 {observed_session}。{reason}"
        return session, reason

    from mino_nexus.loop.observe.session_persist import stamp_session_observation
    stamp_session_observation(ctx, {**row, "ok": True, "session": "unknown", "identity": "unknown"})
    update_auth_state(ctx, device_session="unknown")
    update_auth_state(ctx, device_session_source="screen")
    return "unknown", "当前观察未证明登录态，不能沿用凭据或旧会话宣称成功"
