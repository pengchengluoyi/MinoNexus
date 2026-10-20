"""Bot 作用域：按 app / platform 匹配策略（§3.7）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.models.mino_bots import CollabTopic, ProjectBot
from mino_nexus.services.case_store import get_case
from mino_nexus.services.collab_policy import policy_for_topic



def _norm_platform(value: str) -> str:
    from mino_nexus.core.platform_contract import target_platform
    return target_platform(value)


def matching_bot(db, project_id: str, app_id: str) -> ProjectBot | None:
    pid = str(project_id or "").strip()
    aid = str(app_id or "").strip()
    rows = (
        db.query(ProjectBot)
        .filter_by(project_id=pid, status="active")
        .order_by(ProjectBot.created_at)
        .all()
    )
    if not rows:
        return None
    scoped: list[ProjectBot] = []
    generic: list[ProjectBot] = []
    for bot in rows:
        apps = [str(x).strip() for x in (bot.app_ids or []) if str(x).strip()]
        if apps:
            if aid and aid in apps:
                scoped.append(bot)
        else:
            generic.append(bot)
    if scoped:
        for bot in scoped:
            plats = bot.platforms if isinstance(bot.platforms, list) else []
            po = bot.policy_override if isinstance(bot.policy_override, dict) else {}
            if plats or po:
                return bot
        return scoped[0]
    return generic[0] if generic else None


def resolve_topic_policy(db, topic: CollabTopic) -> dict[str, Any]:
    app_id = str(topic.app_id or "").strip()
    bot = matching_bot(db, topic.project_id, app_id)
    override = bot.policy_override if bot and isinstance(bot.policy_override, dict) else None
    pol = policy_for_topic(topic.project_id, app_id, override)
    platforms = bot.platforms if bot and isinstance(bot.platforms, list) else []
    pol["_bot_id"] = str(bot.bot_id) if bot else ""
    pol["_bot_platforms"] = [_norm_platform(x) for x in platforms if str(x).strip()]
    return pol


def platform_allowed(case_platform: str, allowed: list[str]) -> bool:
    from mino_nexus.core.platform_contract import platform_set
    plat = _norm_platform(case_platform)
    return not allowed or plat in platform_set(allowed)


def filter_case_ids_by_platform(
    project_id: str,
    case_ids: list[str],
    allowed_platforms: list[str],
) -> tuple[list[str], list[str]]:
    if not allowed_platforms:
        return list(case_ids), []
    kept: list[str] = []
    skipped: list[str] = []
    for cid in case_ids:
        case = get_case(project_id, cid) or {}
        from mino_nexus.core.platform_contract import covered_platforms, platform_set
        try:
            matches = bool(covered_platforms(case) & set(platform_set(allowed_platforms)))
        except ValueError:
            matches = False
        if matches:
            kept.append(cid)
        else:
            skipped.append(cid)
    return kept, skipped


def execution_env_allowed(policy: dict[str, Any], *, env_profile: str = "test", surface: str = "") -> bool:
    profiles = policy.get("execute_env_profiles")
    if not isinstance(profiles, list) or not profiles:
        profiles = ["test"]
    prof = str(env_profile or "test").strip()
    if prof and prof not in [str(x) for x in profiles]:
        return False
    surfaces = policy.get("execute_surfaces")
    if isinstance(surfaces, list) and surfaces:
        surf = str(surface or "").strip()
        if not surf or surf not in [str(x) for x in surfaces]:
            return False
    return True
