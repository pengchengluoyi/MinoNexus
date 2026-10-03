"""创建回归任务并落库。逐条执行在 case_exec。"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Optional

from mino_nexus.services import app_automation as aas
from mino_nexus.services import project_store as ps
from mino_nexus.services import run_store
from mino_nexus.services import ui_devices
from mino_nexus.ai.llm_client import resolve_regression_provider
from mino_nexus.core.log import SLog
from mino_nexus.loop.observe.agent_stream import emit_testing_task
from mino_nexus.loop.observe.persist_run_finish import persist_run_finish
from mino_nexus.runtime.run_context import device_platform_kind

TAG = "CaseRunner"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_sns(sn: str = "", sns: Optional[list[str]] = None) -> list[str]:
    raw: list[str] = []
    if sn:
        raw.append(str(sn).strip())
    for item in sns or []:
        raw.append(str(item or "").strip())
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _instruction_case(instruction: str) -> dict[str, Any]:
    text = str(instruction or "").strip()
    cid = f"chat-{run_store.new_run_id()[3:]}"
    return {
        "case_id": cid,
        "name": (text[:80] or cid),
        "precondition": "",
        "steps": [text] if text else [],
        "expected": [],
        "steps_raw": text,
        "expected_raw": "",
        "source": "copilot",
    }


def _case_ui_automation(case: dict[str, Any]) -> dict[str, Any]:
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    ua = meta.get("ui_automation") if isinstance(meta.get("ui_automation"), dict) else None
    if isinstance(ua, dict) and "coverable" in ua:
        return ua
    from mino_nexus.services.case_step_key_compiler import compile_case_step_program_keys

    compiled = compile_case_step_program_keys(case)
    row = compiled.get("ui_automation")
    return dict(row) if isinstance(row, dict) else {"coverable": True, "blocked_by": []}


def _reject_uncoverable_cases(cases: list[dict[str, Any]]) -> None:
    blocked: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        ua = _case_ui_automation(case)
        if ua.get("coverable") is not False:
            continue
        items = [x for x in (ua.get("blocked_by") or []) if isinstance(x, dict)]
        blocked.append(
            {
                "case_id": str(case.get("case_id") or ""),
                "name": str(case.get("name") or "")[:80],
                "blocked_by": items,
            }
        )
    if blocked:
        raise UiNotCoverable(blocked)


def _pick_online_sns() -> list[str]:
    return [str(d.get("sn") or "") for d in ui_devices.ui_devices() if d.get("status") == "online" and d.get("sn")]


def _platform_of(sn: str, fallback: str = "android") -> str:
    for d in ui_devices.ui_devices():
        if d.get("sn") == sn:
            return device_platform_kind(str(d.get("type") or ""), d.get("channels"), sn=sn)
    return fallback


def _sn_for_case_index(
    index: int,
    device_sns: list[str],
    *,
    coverage: str,
) -> str:
    """once + 多机：轮询分片；单机或 per_device 外层已展开。"""
    if not device_sns:
        return ""
    cov = str(coverage or "once").strip().lower()
    if cov == "once" and len(device_sns) > 1:
        return device_sns[index % len(device_sns)]
    return device_sns[0]


def _worker_sns_for_cases(cases: list[dict[str, Any]], doc: dict[str, Any]) -> list[str]:
    """本 run 上实际有 case 绑定的设备列表（保持 sns 顺序）。"""
    bound = {str(c.get("sn") or "").strip() for c in cases if str(c.get("sn") or "").strip()}
    if not bound:
        head = str(doc.get("sn") or "").strip()
        return [head] if head else []
    order: list[str] = []
    for sn in list(doc.get("sns") or []):
        s = str(sn or "").strip()
        if s and s in bound and s not in order:
            order.append(s)
    for s in sorted(bound):
        if s not in order:
            order.append(s)
    return order


def run_cases(
    app: dict[str, Any],
    *,
    sn: str = "",
    sns: Optional[list[str]] = None,
    coverage: str = "",
    platform: str = "android",
    case_ids: Optional[list[str]] = None,
    start_index: int = 0,
    async_exec: bool = True,
    run_type: str = "manual",
    requirement_id: str = "",
    release_id: str = "",
    slot_id: str = "",
    instruction: str = "",
    provider_id: str = "",
    playwright_headless: bool = True,
    env_profile: str = "",
    env_surface: str = "",
    action_scheme: str = "visual",
    plugin_user_id: str = "",
) -> dict[str, Any]:
    t0 = time.perf_counter()
    phases: list[tuple[str, int]] = []

    def mark(name: str) -> None:
        phases.append((name, int((time.perf_counter() - t0) * 1000)))

    device_sns = _normalize_sns(sn, sns)
    cov = str(coverage or "").strip().lower()
    if cov not in ("", "once", "per_device"):
        raise ValueError("coverage 必须是 once 或 per_device")
    if device_sns and (not cov or len(device_sns) == 1):
        cov = "once"
    cov = cov or "once"

    all_cases = aas.list_app_cases(app)
    instruction = str(instruction or "").strip()
    if instruction:
        cases = [_instruction_case(instruction)]
        if not str(run_type or "").strip() or str(run_type).lower() == "manual":
            run_type = "copilot"
    elif case_ids:
        by_id = {str(c.get("case_id")): c for c in all_cases if c.get("case_id")}
        cases = [by_id[str(cid)] for cid in case_ids if str(cid) in by_id]
        missing = [cid for cid in case_ids if str(cid) not in by_id]
        if missing:
            SLog.w(TAG, f"missing case_ids: {missing[:5]} (total {len(missing)})")
    else:
        cases = list(all_cases)
    if start_index > 0:
        cases = cases[start_index:]
    if not cases:
        raise ValueError("没有可执行的用例草稿。请先在流程里生成用例，或指定 case_ids。")
    if not instruction:
        _reject_uncoverable_cases(cases)
    mark("cases")

    roster = ui_devices.ui_devices()

    def platform_of(dsn: str) -> str:
        for dev in roster:
            if dev.get("sn") == dsn:
                return device_platform_kind(str(dev.get("type") or ""), dev.get("channels"), sn=dsn)
        return platform

    platforms_by_sn: dict[str, str] = {dsn: platform_of(dsn) for dsn in device_sns}
    running = run_store.running_index()
    for dsn in device_sns:
        from mino_nexus.runtime.run_context import is_web_slot

        if is_web_slot(dsn, platforms_by_sn.get(dsn, "")):
            _ensure_web_parallel_capacity(dsn)
            continue
        busy_ids = running.get(dsn) or []
        if busy_ids:
            raise DeviceBusy(dsn, busy_ids[0])
    mark("devices")

    if not device_sns:
        device_sns = [
            str(d.get("sn") or "")
            for d in roster
            if d.get("status") == "online" and d.get("sn")
        ][:1]

    platforms_by_sn = {dsn: platform_of(dsn) for dsn in device_sns}
    task_platform = platform
    if device_sns and len(set(platforms_by_sn.values())) == 1:
        task_platform = platforms_by_sn[device_sns[0]]
    elif len(set(platforms_by_sn.values())) > 1:
        task_platform = "mixed"

    cfg = aas.get_automation_config(app)
    project_id = str(app.get("project_id") or "")
    env_doc: dict[str, Any] = {}
    if project_id:
        try:
            env_doc = ps.project_env(project_id)
        except KeyError:
            env_doc = {}
    from mino_nexus.services.project_env import resolve_run_env_profile

    plat_for_pkg = task_platform if task_platform in ("android", "ios", "web") else "android"
    resolved_env = resolve_run_env_profile(
        env_doc,
        requested=str(env_profile or "").strip(),
        automation_default=str(cfg.get("env_profile") or "").strip(),
    )
    env_surface_id = str(env_surface or "").strip()
    package = aas.package_for_app(
        app,
        env_profile=resolved_env,
        platform=plat_for_pkg,
        surface=env_surface_id,
    )
    playbook = aas.get_playbook(app)
    run_id = run_store.new_run_id()
    seeded = [
        run_store.seed_case(
            run_id,
            c,
            i,
            sn=_sn_for_case_index(i, device_sns, coverage=cov) if cov != "per_device" else "",
            coverage=cov,
        )
        for i, c in enumerate(cases)
    ]
    if cov == "per_device" and len(device_sns) > 1:
        seeded = [
            run_store.seed_case(run_id, c, i, sn=dsn, coverage=cov)
            for dsn in device_sns
            for i, c in enumerate(cases)
        ]

    provider, gate = resolve_regression_provider(str(provider_id or "").strip() or None)
    mark("assembled")
    doc: dict[str, Any] = {
        "run_id": run_id,
        "task_id": run_id,
        "engine": "agent",
        "run_type": run_type or "manual",
        "app_id": str(app.get("id") or ""),
        "app_name": str(app.get("name") or ""),
        "sn": device_sns[0] if device_sns else "",
        "sns": device_sns,
        "coverage": cov,
        "platform": task_platform,
        "platforms_by_sn": platforms_by_sn,
        "env_profile": resolved_env,
        "env_surface": env_surface_id,
        "action_scheme": "dom" if str(action_scheme or "").strip().lower() == "dom" else "visual",
        "plugin_user_id": str(plugin_user_id or "").strip(),
        "package": package,
        "playbook": playbook if isinstance(playbook, dict) else {},
        "requirement_id": str(requirement_id or "").strip(),
        "release_id": str(release_id or "").strip(),
        "slot_id": str(slot_id or "").strip(),
        "provider_id": (provider or {}).get("id") or gate.get("provider_id") or "",
        "provider_name": (provider or {}).get("name") or "",
        "model_name": (provider or {}).get("model") or "",
        "playwright_headless": bool(playwright_headless),
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "cases": seeded,
        "error": "",
        "total": len(seeded),
        "completed": 0,
        "passed": 0,
        "failed": 0,
        "boot_ms": phases[-1][1] if phases else 0,
        "boot_phases": [{"name": name, "ms": ms} for name, ms in phases],
    }
    run_store.put(doc)
    mark("persisted")
    doc["boot_ms"] = phases[-1][1] if phases else 0
    doc["boot_phases"] = [{"name": name, "ms": ms} for name, ms in phases]
    SLog.i(
        TAG,
        "boot run=%s %s"
        % (run_id, " ".join(f"{name}={ms}" for name, ms in phases)),
    )
    emit_testing_task({"event": "task_created", "run_id": run_id, "task_id": run_id, "app_id": doc["app_id"]})

    fail_reason = ""
    if not device_sns:
        fail_reason = "没有在线设备。请在 Studio 安装并连接 Scout，再选一台设备下发。"
    elif not provider:
        fail_reason = gate.get("reason") or "未配置用例执行大模型（Studio → 密钥 → 大模型 Key → 可用 + 用例）"

    if fail_reason:
        for case in doc["cases"]:
            case["status"] = "fail"
            case["summary"] = fail_reason
            case["error"] = fail_reason
        doc["error"] = fail_reason
        run_store.put(doc)
        finished = run_store.finish(run_id, status="failed", error=fail_reason)
        persist_run_finish(finished)
        return run_store.to_task_json(finished)

    worker = threading.Thread(
        target=_run_in_background,
        kwargs={"run_id": run_id, "package": package, "playbook": playbook, "provider_id": doc["provider_id"]},
        daemon=True,
        name=f"case-run-{run_id}",
    )
    worker.start()
    if not async_exec:
        worker.join(timeout=600)
        latest = run_store.get(run_id) or doc
        return run_store.to_task_json(latest)
    return run_store.to_task_json(doc)



def _run_in_background(**kwargs):
    """后台执行在 case_exec。这里只转一手，避免和任务创建互相导入。"""
    from mino_nexus.loop.case_exec import _run_in_background as _impl
    return _impl(**kwargs)



def run_explore(
    app: dict[str, Any],
    *,
    sn: str = "",
    max_steps: int = 80,
    max_idle_steps: int = 15,
    instruction: str = "",
    provider_id: str = "",
    async_exec: bool = True,
    platform: str = "android",
    playwright_headless: bool = True,
    env_profile: str = "",
    env_surface: str = "",
    plugin_user_id: str = "",
) -> dict[str, Any]:
    """发起应用探索：LLM 自由操作，被动采集拓展 Screen Atlas。"""
    from mino_nexus.loop.explore_case import build_explore_case

    device_sns = _normalize_sns(sn, None)
    if not device_sns:
        device_sns = _pick_online_sns()[:1]
    for dsn in device_sns:
        from mino_nexus.runtime.run_context import is_web_slot

        if is_web_slot(dsn, _platform_of(dsn, platform)):
            _ensure_web_parallel_capacity(dsn)
            continue
        busy = run_store.busy_task_for_sn(dsn)
        if busy:
            raise DeviceBusy(dsn, busy)

    explore_case = build_explore_case(
        instruction=str(instruction or "").strip(),
        max_steps=max_steps,
        max_idle_steps=max_idle_steps,
    )
    cfg = aas.get_automation_config(app)
    plat_for_pkg = platform if platform in ("android", "ios", "web") else "android"
    resolved_env = str(env_profile or cfg.get("env_profile") or "test").strip()
    env_surface_id = str(env_surface or "").strip()
    package = aas.package_for_app(
        app,
        env_profile=resolved_env,
        platform=plat_for_pkg,
        surface=env_surface_id,
    )
    playbook = aas.get_playbook(app)
    run_id = run_store.new_run_id()
    seeded = run_store.seed_case(run_id, explore_case, 0, sn=device_sns[0] if device_sns else "", coverage="once")
    provider, gate = resolve_regression_provider(str(provider_id or "").strip() or None)
    doc: dict[str, Any] = {
        "run_id": run_id,
        "task_id": run_id,
        "engine": "agent",
        "run_type": "explore",
        "session_kind": "explore",
        "app_id": str(app.get("id") or ""),
        "app_name": str(app.get("name") or ""),
        "sn": device_sns[0] if device_sns else "",
        "sns": device_sns,
        "coverage": "once",
        "platform": platform,
        "env_profile": resolved_env,
        "env_surface": env_surface_id,
        "plugin_user_id": str(plugin_user_id or "").strip(),
        "package": package,
        "playbook": playbook if isinstance(playbook, dict) else {},
        "provider_id": (provider or {}).get("id") or gate.get("provider_id") or "",
        "provider_name": (provider or {}).get("name") or "",
        "model_name": (provider or {}).get("model") or "",
        "playwright_headless": bool(playwright_headless),
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "cases": [seeded],
        "error": "",
        "total": 1,
        "completed": 0,
        "passed": 0,
        "failed": 0,
        "explore": {
            "max_steps": int(explore_case.get("max_steps") or 80),
            "max_idle_steps": int(explore_case.get("max_idle_steps") or 15),
        },
    }
    run_store.put(doc)
    persist_run_finish(doc)
    emit_testing_task({"event": "task_created", "run_id": run_id, "task_id": run_id, "app_id": doc["app_id"], "kind": "explore"})

    fail_reason = ""
    if not device_sns:
        fail_reason = "没有在线设备。请在 Studio 连接 Scout 后再发起探索。"
    elif device_sns and not any(
        d.get("status") == "online" and d.get("sn") == device_sns[0]
        for d in ui_devices.ui_devices()
    ):
        fail_reason = f"设备 {device_sns[0]} 已离线，请连接 Scout 后重试。"
    elif not provider:
        fail_reason = gate.get("reason") or "未配置用例执行大模型（Studio → 密钥 → 大模型 Key）"

    if fail_reason:
        run_store.patch_case(run_id, explore_case["case_id"], status="fail", summary=fail_reason, error=fail_reason)
        finished = run_store.finish(run_id, status="failed", error=fail_reason)
        persist_run_finish(finished)
        return run_store.to_task_json(finished)

    worker = threading.Thread(
        target=_run_in_background,
        kwargs={"run_id": run_id, "package": package, "playbook": playbook, "provider_id": doc["provider_id"]},
        daemon=True,
        name=f"explore-run-{run_id}",
    )
    worker.start()
    if not async_exec:
        worker.join(timeout=1200)
        latest = run_store.get(run_id) or doc
        return run_store.to_task_json(latest)
    return run_store.to_task_json(doc)


def retry_failed(task_id: str, *, sn: str = "") -> dict[str, Any]:
    doc = run_store.get(task_id)
    if not doc:
        return {"ok": False, "code": 404, "reason": "任务不存在"}
    failed_ids = [
        str(c.get("case_id"))
        for c in (doc.get("cases") or [])
        if str(c.get("status") or "") in ("fail", "blocked", "declined")
    ]
    if not failed_ids:
        return {"ok": False, "code": 400, "reason": "没有失败用例可重跑"}
    app = ps.require_app(str(doc.get("app_id") or ""))
    snapshot = run_cases(
        app,
        sn=sn or str(doc.get("sn") or ""),
        sns=[sn] if sn else list(doc.get("sns") or []),
        coverage=str(doc.get("coverage") or "once"),
        platform=str(doc.get("platform") or "android"),
        case_ids=failed_ids,
        run_type=str(doc.get("run_type") or "manual"),
        requirement_id=str(doc.get("requirement_id") or ""),
        release_id=str(doc.get("release_id") or ""),
        provider_id=str(doc.get("provider_id") or ""),
        playwright_headless=bool(doc.get("playwright_headless", True)),
        env_profile=str(doc.get("env_profile") or ""),
        env_surface=str(doc.get("env_surface") or ""),
        action_scheme=str(doc.get("action_scheme") or "visual"),
        plugin_user_id=str(doc.get("plugin_user_id") or ""),
    )
    return {"ok": True, "code": 200, "case_ids": failed_ids, "data": snapshot}


def _ensure_web_parallel_capacity(sn: str) -> None:
    from mino_nexus.runtime.run_context import WEB_PLAYWRIGHT_PARALLEL_LANES

    ids = run_store.running_run_ids_for_sn(sn, limit=WEB_PLAYWRIGHT_PARALLEL_LANES + 1)
    active = len(ids)
    if active >= WEB_PLAYWRIGHT_PARALLEL_LANES:
        raise WebSlotFull(sn, active, WEB_PLAYWRIGHT_PARALLEL_LANES)


class UiNotCoverable(Exception):
    def __init__(self, blocked: list[dict[str, Any]]):
        self.blocked = list(blocked or [])
        bits: list[str] = []
        for row in self.blocked:
            cid = str(row.get("case_id") or "")
            for item in row.get("blocked_by") or []:
                if not isinstance(item, dict):
                    continue
                step = item.get("case_step")
                clause = str(item.get("source_clause") or "").strip()
                message = str(item.get("user_message") or "").strip()
                if message:
                    bits.append(message)
                    continue
                reason = str(item.get("reason") or "")
                bits.append(f"{cid} 第{step}步「{clause}」({reason})")
        msg = "无法 UI 自动化测试：" + ("；".join(bits[:8]) if bits else "存在写不到具体事件的步骤")
        super().__init__(msg)


class DeviceBusy(Exception):
    def __init__(self, sn: str, busy_task_id: str):
        super().__init__(f"device busy: {sn}")
        self.sn = sn
        self.busy_task_id = busy_task_id


class WebSlotFull(Exception):
    def __init__(self, sn: str, active: int, limit: int):
        super().__init__(f"web slot full: {sn}")
        self.sn = sn
        self.active = active
        self.limit = limit
