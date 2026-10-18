"""创建回归任务并落库。逐条执行在 case_exec。"""
from __future__ import annotations

from mino_nexus.core.maintenance_gate import admitted

from mino_nexus.action_space.scheme import normalize_action_scheme
from mino_nexus.core.execution_user import user_thread, owned_job, require_user_id


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


@admitted
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
    action_scheme: str = "dom_preferred",
    plugin_user_id: str = "",
    execution_mode: str = "legacy",
    client_request_id: str = "",
    derived_from: str = "",
) -> dict[str, Any]:
    t0 = time.perf_counter()
    phases: list[tuple[str, int]] = []

    def mark(name: str) -> None:
        phases.append((name, int((time.perf_counter() - t0) * 1000)))

    action_scheme = normalize_action_scheme(action_scheme)
    require_user_id(plugin_user_id or None)
    from mino_nexus.services.case_compilation import digest
    if client_request_id and (len(client_request_id) > 160 or not all(character.isalnum() or character in "_.:-" for character in client_request_id)):
        raise ValueError("run_request_id_invalid")
    request_digest = digest({"app_id": app.get("id"), "sn": sn, "sns": sns, "coverage": coverage,
        "platform": platform, "case_ids": case_ids, "start_index": start_index, "run_type": run_type,
        "requirement_id": requirement_id, "release_id": release_id, "slot_id": slot_id, "instruction": instruction,
        "provider_id": provider_id, "playwright_headless": playwright_headless, "env_profile": env_profile,
        "env_surface": env_surface, "action_scheme": action_scheme, "execution_mode": execution_mode})
    if client_request_id:
        from mino_nexus.core.database import session_scope
        from mino_nexus.models.run_admission import RunAdmission
        with session_scope() as database:
            receipt = database.query(RunAdmission).filter_by(owner_user_id=require_user_id(), request_id=client_request_id).one_or_none()
            if receipt:
                if receipt.request_digest != request_digest:
                    raise ValueError("run_request_conflict")
                original = run_store.get(receipt.run_id)
                if original is None:
                    raise ValueError("run_receipt_target_missing")
                return run_store.to_task_json(original)
    execution_mode = "legacy" if execution_mode in {"", "agent"} else execution_mode
    if execution_mode not in {"legacy", "v5"}:
        raise ValueError("未知执行模式")
    from mino_nexus.services.access_scope import require_owned_device
    device_sns = _normalize_sns(sn, sns)
    for owned_sn in device_sns:
        require_owned_device(owned_sn)
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
    elif case_ids is not None:
        by_id = {str(c.get("case_id")): c for c in all_cases if c.get("case_id")}
        cases = [by_id[str(cid)] for cid in case_ids if str(cid) in by_id]
        missing = [cid for cid in case_ids if str(cid) not in by_id]
        if missing:
            raise ValueError("所选用例已删除或不属于当前项目，请重新选择；本次未创建任务。")
    else:
        cases = list(all_cases)
    if start_index > 0:
        cases = cases[start_index:]
    if not cases:
        raise ValueError("没有可执行的用例草稿。请先在流程里生成用例，或指定 case_ids。")
    requested_total = len(cases)
    skipped_cases: list[dict[str, Any]] = []
    if not instruction and execution_mode != "v5":
        from mino_nexus.services.case_preflight import partition_cases

        cases, preflight = partition_cases(cases)
        skipped_cases = preflight["blocked"]
        if not cases:
            raise UiNotCoverable(skipped_cases)
    if execution_mode == "v5":
        if instruction or not device_sns or not playwright_headless:
            raise ValueError("V5 需要已发布文字用例、固定 Web 节点和 headless")
        if any(not c.get("v5_publication") or c.get("v5_plan_state") != "published" for c in cases):
            raise ValueError("V5 用例尚未发布或需要重新核验")
    mark("cases")

    from mino_nexus.services.device_targets import select
    device_sns, device_targets = select(app, device_sns, surface=env_surface, profile=env_profile,
                                        headless=playwright_headless, action_scheme=action_scheme, allow_queue=execution_mode == "v5")
    if execution_mode == "v5" and any(device_targets[s].get("platform") != "web" or
        s != "web" + str(device_targets[s].get("node_id") or "") for s in device_sns):
        raise ValueError("V5 必须固定到声明 Web 槽的节点")
    platforms_by_sn = {dsn: str(device_targets[dsn]["platform"]) for dsn in device_sns}
    mark("devices")
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

    plat_for_pkg = task_platform
    if task_platform == "desktop":
        native_os = device_targets[device_sns[0]]["os"]
        plat_for_pkg = {"macos": "mac", "windows": "pc", "linux": "linux"}[native_os]
        env_surface = device_targets[device_sns[0]].get("target_surface") or env_surface
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

    from mino_nexus.services.access_scope import require_owned_device
    for owned_sn in device_sns:
        require_owned_device(owned_sn)
    provider, gate = ({}, {}) if execution_mode == "v5" else resolve_regression_provider(str(provider_id or "").strip() or None)
    from mino_nexus.loop.step_pointer import compute_case_wall_budget_sec
    batch_budget = sum(compute_case_wall_budget_sec(case) + 130 for case in seeded)
    mark("assembled")
    doc: dict[str, Any] = {
        "run_id": run_id,
        "client_request_id": client_request_id,
        "request_digest": request_digest,
        "derived_from": derived_from,
        "environment_baseline_digest": digest({"profile": resolved_env, "surface": env_surface_id,
            "package": package, "environment": (env_doc.get("profiles") or {}).get(resolved_env),
            "channels": env_doc.get("channels") or []}),
        "execution_policy": {"version": 1, "batch_deadline_at": time.time() + batch_budget,
            "batch_budget_seconds": batch_budget, "resource_wait_seconds": 120, "cleanup_seconds": 10},
        "task_id": run_id,
        "engine": "agent",
        "execution_mode": execution_mode,
        "run_type": run_type or "manual",
        "app_id": str(app.get("id") or ""),
        "app_name": str(app.get("name") or ""),
        "sn": device_sns[0] if device_sns else "",
        "sns": device_sns,
        "coverage": cov,
        "platform": task_platform,
        "platforms_by_sn": platforms_by_sn,
        "device_targets": device_targets,
        "env_profile": resolved_env,
        "env_surface": env_surface_id,
        "action_scheme": normalize_action_scheme(action_scheme),
        "owner_user_id": require_user_id(plugin_user_id or None),
        "plugin_user_id": require_user_id(plugin_user_id or None),
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
        "requested_total": requested_total,
        "skipped_cases": skipped_cases,
        "error": "",
        "total": len(seeded),
        "completed": 0,
        "passed": 0,
        "failed": 0,
        "boot_ms": phases[-1][1] if phases else 0,
        "boot_phases": [{"name": name, "ms": ms} for name, ms in phases],
    }
    persisted = run_store.put(doc)
    if persisted.get("run_id") != run_id:
        return run_store.to_task_json(persisted)
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
        fail_reason = "没有在线设备。请在 Workbench 安装并连接 Scout，再选一台设备下发。"
    elif not provider and execution_mode != "v5":
        fail_reason = gate.get("reason") or "未配置用例执行大模型（Workbench → 大模型设置 → 可用 + 用例）"

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

    worker = user_thread(
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



@admitted
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

    require_user_id(plugin_user_id or None)
    from mino_nexus.services.access_scope import require_owned_device
    device_sns = _normalize_sns(sn, None)
    for owned_sn in device_sns:
        require_owned_device(owned_sn)
    from mino_nexus.services.device_targets import select
    device_sns, device_targets = select(app, device_sns, surface=env_surface, profile=env_profile,
                                        headless=playwright_headless)
    platform = str(device_targets[device_sns[0]]["platform"])
    explore_case = build_explore_case(
        platform=platform,
        instruction=str(instruction or "").strip(),
        max_steps=max_steps,
        max_idle_steps=max_idle_steps,
    )
    cfg = aas.get_automation_config(app)
    plat_for_pkg = platform
    if platform == "desktop":
        native_os = device_targets[device_sns[0]]["os"]
        plat_for_pkg = {"macos": "mac", "windows": "pc", "linux": "linux"}[native_os]
        env_surface = device_targets[device_sns[0]].get("target_surface") or env_surface
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
    from mino_nexus.services.access_scope import require_owned_device
    for owned_sn in device_sns:
        require_owned_device(owned_sn)
    provider, gate = resolve_regression_provider(str(provider_id or "").strip() or None)
    doc: dict[str, Any] = {
        "run_id": run_id,
        "task_id": run_id,
        "engine": "agent",
        "run_type": "explore",
        "session_kind": "explore",
        "action_scheme": normalize_action_scheme(None),
        "app_id": str(app.get("id") or ""),
        "app_name": str(app.get("name") or ""),
        "sn": device_sns[0] if device_sns else "",
        "sns": device_sns,
        "coverage": "once",
        "platform": platform,
        "platforms_by_sn": {s: device_targets[s]["platform"] for s in device_sns},
        "device_targets": device_targets,
        "env_profile": resolved_env,
        "env_surface": env_surface_id,
        "owner_user_id": require_user_id(plugin_user_id or None),
        "plugin_user_id": require_user_id(plugin_user_id or None),
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
        fail_reason = "没有在线设备。请在 Workbench 连接 Scout 后再发起探索。"
    elif device_sns and not any(
        d.get("status") == "online" and d.get("sn") == device_sns[0]
        for d in ui_devices.ui_devices()
    ):
        fail_reason = f"设备 {device_sns[0]} 已离线，请连接 Scout 后重试。"
    elif not provider:
        fail_reason = gate.get("reason") or "未配置用例执行大模型（Workbench → 大模型设置）"

    if fail_reason:
        run_store.patch_case(run_id, explore_case["case_id"], status="fail", summary=fail_reason, error=fail_reason)
        finished = run_store.finish(run_id, status="failed", error=fail_reason)
        persist_run_finish(finished)
        return run_store.to_task_json(finished)

    worker = user_thread(
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


@admitted
def retry_failed(task_id: str, *, sn: str = "", execution_unit_ids=None, client_request_id="") -> dict[str, Any]:
    from mino_nexus.services.run_retry import create, eligibility
    if sn:
        return {"ok": False, "code": 409, "reason": "复跑必须保持原执行目标", "reason_code": "retry_target_changed"}
    try:
        doc, created = create(task_id, execution_unit_ids or [], client_request_id)
    except KeyError:
        return {"ok": False, "code": 404, "reason": "任务不存在"}
    except ValueError as exc:
        try:
            assessment = eligibility(task_id)
        except KeyError:
            return {"ok": False, "code": 404, "reason": "任务不存在"}
        return {"ok": False, "code": 409, "reason": "复跑准入未通过，请查看阻塞原因",
                "reason_code": str(exc), "eligibility": assessment}
    if created:
        worker = user_thread(target=_run_in_background, kwargs={"run_id": doc["run_id"],
            "package": doc["package"], "playbook": doc.get("playbook") or {}, "provider_id": doc.get("provider_id", "")},
            daemon=True, name=f"case-retry-{doc['run_id']}")
        worker.start()
    return {"ok": True, "data": run_store.to_task_json(doc), "case_ids": [c["case_id"] for c in doc["cases"]]}


def _ensure_web_parallel_capacity(sn: str) -> None:
    from mino_nexus.runtime.run_context import WEB_PLAYWRIGHT_PARALLEL_LANES

    ids = run_store.running_run_ids_for_sn(sn, limit=WEB_PLAYWRIGHT_PARALLEL_LANES + 1)
    active = len(ids)
    if active >= WEB_PLAYWRIGHT_PARALLEL_LANES:
        raise WebSlotFull(sn, active, WEB_PLAYWRIGHT_PARALLEL_LANES)


class UiNotCoverable(Exception):
    def __init__(self, blocked: list[dict[str, Any]]):
        from mino_nexus.services.case_preflight import blocked_message

        self.blocked = list(blocked or [])
        super().__init__("所选用例均无法 UI 自动化，本次未创建任务。\n" + blocked_message(self.blocked))


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
