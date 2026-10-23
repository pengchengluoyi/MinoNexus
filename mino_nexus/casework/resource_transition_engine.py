"""配置化资源转移：规则表 → 效果 → 审计 + session log。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.catalog.resource_transition_seed import list_builtin_rules

TAG = "ResourceTransitionEngine"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _platform(ctx: Any) -> str:
    from mino_nexus.core.platform_contract import target_platform

    return target_platform(getattr(ctx, "platform", None))


def _rules_for_trigger(trigger_id: str, platform: str) -> list[dict[str, Any]]:
    tid = str(trigger_id or "").strip()
    if not tid:
        return []
    plat = str(platform or "any").strip().lower() or "any"
    from mino_nexus.core.database import SessionLocal
    from mino_nexus.models.resource_ops import ResourceTransitionRule

    db = SessionLocal()
    try:
        rows = (
            db.query(ResourceTransitionRule)
            .filter(
                ResourceTransitionRule.trigger_id == tid,
                ResourceTransitionRule.enabled == 1,
            )
            .all()
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            rp = str(row.platform or "any").strip().lower()
            if rp not in ("any", plat):
                continue
            out.append(
                {
                    "rule_id": row.rule_id,
                    "trigger_id": row.trigger_id,
                    "effects_json": list(row.effects_json or []),
                }
            )
        if out:
            return out
    finally:
        db.close()

    fallback: list[dict[str, Any]] = []
    for row in list_builtin_rules():
        if str(row.get("trigger_id") or "") != tid:
            continue
        rp = str(row.get("platform") or "any").strip().lower()
        if rp not in ("any", plat):
            continue
        fallback.append(row)
    return fallback


def _apply_effect(ctx: Any, effect: dict[str, Any], *, package_id: str = "", meta: dict[str, Any]) -> bool | None:
    from mino_nexus.casework.resource_transition import _apply_device_login, emit_device_logout

    action = str(effect.get("action") or "").strip()
    src = str(effect.get("source") or meta.get("source") or "rule")[:32]
    pkg = str(package_id or meta.get("package_id") or "").strip()
    if action == "device_logout":
        emit_device_logout(
            ctx,
            package_id=pkg,
            source=src,
            stale_reason=str(effect.get("stale_reason") or meta.get("stale_reason") or "")[:200],
            sync_account=bool(effect.get("sync_account", True)),
        )
    elif action == "device_login":
        return _apply_device_login(ctx, package_id=pkg, source=src)


def _write_audit(
    ctx: Any,
    *,
    trigger_id: str,
    source: str,
    platform: str,
    payload: dict[str, Any],
) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.resource_ops import ResourceTransitionAudit

    sn = str(getattr(ctx, "sn", "") or "").strip()
    pkg = str(payload.get("package_id") or getattr(ctx, "target_package", "") or "").strip()
    with session_scope() as db:
        db.add(
            ResourceTransitionAudit(
                run_id=str(getattr(ctx, "run_id", "") or "")[:80],
                case_id=str(getattr(ctx, "case_id", "") or "")[:64],
                session_id=str(getattr(ctx, "session_id", "") or "").strip(),
                sn=sn[:64],
                package_id=pkg[:128],
                trigger_id=str(trigger_id)[:64],
                source=str(source or "")[:32],
                platform=str(platform or "")[:16],
                payload_json=dict(payload),
                created_at=_now(),
            )
        )


def _mirror_session_log(ctx: Any, payload: dict[str, Any]) -> None:
    from mino_nexus.records.eventlog.writer import active_writer

    sid = str(getattr(ctx, "session_id", "") or "").strip()
    if not sid:
        SLog.w(TAG, "资源变更缺少 session_id，无法关联会话轨迹")
        return
    writer = active_writer()
    if writer is None or writer.session_id != sid:
        from mino_nexus.records.eventlog import store as session_store

        try:
            session_store.append_event(
                session_id=sid,
                type="resource/transition",
                payload=payload,
                turn=int(getattr(ctx, "turn", 0) or 0),
                phase=str(getattr(ctx, "phase", "") or ""),
            )
        except Exception as exc:
            SLog.w(TAG, f"session append resource/transition: {exc!r}")
        return
    writer.append("resource/transition", payload)


def record_transition(
    ctx: Any,
    trigger_id: str,
    *,
    source: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    """仅审计 + session log（状态已由调用方写入登记簿时使用）。"""
    tid = str(trigger_id or "").strip()
    if not tid:
        return
    plat = _platform(ctx)
    extra = dict(meta or {})
    extra.setdefault("trigger_id", tid)
    extra.setdefault("source", source)
    extra.setdefault("platform", plat)
    extra.setdefault("package_id", str(getattr(ctx, "target_package", "") or ""))
    _write_audit(ctx, trigger_id=tid, source=source, platform=plat, payload=extra)
    _mirror_session_log(ctx, extra)


def fire_transition(
    ctx: Any,
    trigger_id: str,
    *,
    source: str = "",
    package_id: str = "",
    meta: dict[str, Any] | None = None,
) -> bool:
    """按规则表执行转移；返回是否至少应用了一条 effect。"""
    tid = str(trigger_id or "").strip()
    if not tid:
        return False
    if tid == "clear_app_cache":
        # 2026-10-22：clear_app_cache 就是 pm clear（清数据+缓存），等价未登录重置；
        # 旧库里的清数据别名不再产生新的转移规则。
        from mino_nexus.casework.resource_transition import emit_clear_app_cache
        emit_clear_app_cache(ctx, package_id=package_id)
        return True
    plat = _platform(ctx)
    rules = _rules_for_trigger(tid, plat)
    if not rules:
        return False
    extra = dict(meta or {})
    extra.setdefault("trigger_id", tid)
    extra.setdefault("source", source)
    extra.setdefault("platform", plat)
    extra.setdefault("package_id", str(package_id or getattr(ctx, "target_package", "") or ""))
    applied = False
    for rule in rules:
        for effect in rule.get("effects_json") or []:
            if not isinstance(effect, dict):
                continue
            if _apply_effect(ctx, effect, package_id=package_id, meta=extra) is not False:
                applied = True
    if applied:
        audit_payload = {**extra, "rule_ids": [r.get("rule_id") for r in rules]}
        _write_audit(ctx, trigger_id=tid, source=source, platform=plat, payload=audit_payload)
        _mirror_session_log(ctx, audit_payload)
        SLog.i(TAG, f"fire {tid} source={source} platform={plat}")
    return applied


def cap_id_to_trigger(cap_id: str) -> str | None:
    from mino_nexus.catalog.resource_logout_caps import is_logout_capability

    cid = str(cap_id or "").strip()
    if not cid:
        return None
    direct = {
        "clear_app_cache": "clear_app_cache",
    }
    if cid in direct:
        return direct[cid]
    if is_logout_capability(cid):
        return "cap_logout"
    return None


def maybe_fire_cap_transition(ctx: Any, cap_id: str, status: str) -> None:
    if str(status or "").strip().lower() != "pass":
        return
    tid = cap_id_to_trigger(cap_id)
    if not tid:
        return
    fire_transition(ctx, tid, source="capability", meta={"cap_id": cap_id})
