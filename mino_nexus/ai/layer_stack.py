"""四层编排：驱动 → 技能 → 角色 → 触发。绑定可覆盖，目录本身写死。"""
from __future__ import annotations

from typing import Any

DRIVERS: list[dict[str, Any]] = [
    {"id": "adb", "label": "真机 ADB", "kind": "device", "summary": "本机点按、截图、装包"},
    {"id": "claw", "label": "Scout 远程", "kind": "device", "summary": "经 Scout 执行"},
    {"id": "im.send", "label": "IM 发消息", "kind": "channel", "summary": "通道回消息"},
]

SKILL_CATEGORIES: list[dict[str, str]] = [
    {"id": "flow", "label": "流程产出", "desc": "读需求、写脑图和用例"},
    {"id": "device", "label": "设备操作", "desc": "真机上规划、点按、定位和断言"},
    {"id": "channel", "label": "通道对话", "desc": "IM 里回答、下令"},
    {"id": "sync", "label": "外部同步", "desc": "设计稿 / 文档"},
]

_SKILL_ALIASES = {
    "im.dialogue": "im-dialogue",
    "im.defect": "im-defect",
    "agent-decide": "run-case",
    "plan-overview": "run-case",
}


def _normalize_skill_id(skill_id: str) -> str:
    sid = str(skill_id or "").strip()
    return _SKILL_ALIASES.get(sid, sid)


ROLES: list[dict[str, str]] = [
    {"id": "conductor", "label": "分析师"},
    {"id": "req-analyst", "label": "需求分析师"},
    {"id": "mindmap-writer", "label": "脑图编写"},
    {"id": "case-writer", "label": "用例编写"},
    {"id": "req-qa-bm", "label": "需求测试 BM"},
    {"id": "version-qa-bm", "label": "版本测试 BM"},
    {"id": "test-engineer", "label": "测试工程师"},
    {"id": "report-writer", "label": "报告编写"},
    {"id": "doc-keeper", "label": "文档维护"},
    {"id": "im-qa-assistant", "label": "IM 总指挥"},
    {"id": "im-defect-assistant", "label": "IM 缺陷助手"},
    {"id": "knowledge-reviewer", "label": "知识审核员"},
    {"id": "product-expert", "label": "产品专家"},
]

TRIGGERS: list[dict[str, Any]] = [
    {"id": "im_chat", "label": "IM 进线", "summary": "通道里有人私聊或 @机器人", "intents": ["dialogue", "defect"], "live": False},
    {"id": "qa_tick", "label": "继续分析", "summary": "需求 / 版本流程点「继续分析」", "intents": ["default"], "live": True},
    {"id": "case_run", "label": "跑用例", "summary": "对应用和设备下发自动化", "intents": ["default"], "live": True},
    {"id": "settings_chat", "label": "设置页对话", "summary": "角色页里的试对话", "intents": ["default"], "live": True},
]

DEFAULT_SKILL_DRIVERS: dict[str, list[str]] = {
    "im-dialogue": ["im.send"],
    "run-case": ["adb", "claw"],
    "assert-vision": ["adb", "claw"],
}

DEFAULT_ROLE_SKILLS: dict[str, list[str]] = {
    "im-qa-assistant": ["im-dialogue"],
    "im-defect-assistant": ["im-defect"],
    "req-analyst": ["analyze_req", "propose_atlas"],
    "mindmap-writer": ["draft_mindmap"],
    "case-writer": ["draft_cases"],
    "req-qa-bm": ["map_cases", "draft_sign"],
    "version-qa-bm": ["pick_regression", "draft_gate"],
    "test-engineer": ["run-case", "assert-vision"],
    "conductor": [],
}

DEFAULT_TRIGGER_ROLES: dict[str, dict[str, str]] = {
    "im_chat": {"dialogue": "im-qa-assistant", "defect": "im-defect-assistant"},
    "qa_tick": {"default": "conductor"},
    "case_run": {"default": "test-engineer"},
    "settings_chat": {"default": ""},
}

DEFAULT_TRIGGER_SKILLS: dict[str, dict[str, str]] = {
    "im_chat": {"dialogue": "im-dialogue", "defect": "im-defect"},
    "qa_tick": {"default": ""},
    "case_run": {"default": "run-case"},
    "settings_chat": {"default": ""},
}

_INTENT_LABEL = {"dialogue": "问答", "defect": "提缺陷", "default": "默认"}
_SKILL_IDS: set[str] = set()
_ROLE_IDS = {row["id"] for row in ROLES}


def _clean_id_list(raw: Any, allowed: set[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        sid = _normalize_skill_id(str(item or "").strip())
        if not sid or sid in seen or sid not in allowed:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def get_stack() -> dict[str, Any]:
    from mino_nexus.platform.llm.repo import stack_spec

    spec = stack_spec()
    drivers = list(spec.get("drivers") or [])
    categories = list(spec.get("skill_categories") or [])
    skills_src = [row for row in (spec.get("skills") or []) if isinstance(row, dict)]
    roles_src = [row for row in (spec.get("roles") or []) if isinstance(row, dict)]
    triggers_src = [row for row in (spec.get("triggers") or []) if isinstance(row, dict)]
    default_skill_drivers = dict(spec.get("skill_drivers") or {})
    default_role_skills = {rid: list(ids) for rid, ids in (spec.get("role_skills") or {}).items()}
    default_trigger_roles = dict(spec.get("trigger_roles") or {})
    default_trigger_skills = dict(spec.get("trigger_skills") or {})
    intent_label = dict(spec.get("intent_labels") or _INTENT_LABEL)
    skill_ids = {row["id"] for row in skills_src}
    role_ids = {row["id"] for row in roles_src}
    driver_ids = {d["id"] for d in drivers}
    bindings = spec.get("bindings") if isinstance(spec.get("bindings"), dict) else {}

    extra = bindings.get("role_skills")
    extra = extra if isinstance(extra, dict) else {}
    role_skills = {rid: list(ids) for rid, ids in default_role_skills.items()}
    for rid in role_ids:
        if rid in extra:
            role_skills[rid] = _clean_id_list(extra[rid], skill_ids)
    extra_sd = bindings.get("skill_drivers")
    extra_sd = extra_sd if isinstance(extra_sd, dict) else {}
    skill_drivers = {**default_skill_drivers, **{
        sid: _clean_id_list(ids, driver_ids)
        for sid, ids in extra_sd.items() if sid in skill_ids
    }}
    extra_tr = bindings.get("trigger_roles")
    extra_tr = extra_tr if isinstance(extra_tr, dict) else {}
    extra_ts = bindings.get("trigger_skills")
    extra_ts = extra_ts if isinstance(extra_ts, dict) else {}
    trigger_roles = {**default_trigger_roles, **extra_tr}
    trigger_skills = {**default_trigger_skills, **extra_ts}

    skills = []
    for row in skills_src:
        sid = row["id"]
        skills.append({**row, "driver_ids": list(skill_drivers.get(sid) or [])})
    roles = []
    for row in roles_src:
        rid = row["id"]
        ids = list(role_skills.get(rid) or [])
        roles.append({**row, "skill_ids": ids, "skills": [s for s in skills if s["id"] in ids]})
    triggers = []
    for row in triggers_src:
        tid = row["id"]
        triggers.append({
            **row,
            "roles": trigger_roles.get(tid) or {},
            "skills": trigger_skills.get(tid) or {},
            "intent_labels": {key: intent_label.get(key, key) for key in (row.get("intents") or ["default"])},
        })
    return {
        "drivers": drivers,
        "skill_categories": categories,
        "skills": skills,
        "roles": roles,
        "triggers": triggers,
        "skill_drivers": skill_drivers,
        "role_skills": role_skills,
        "trigger_roles": trigger_roles,
        "trigger_skills": trigger_skills,
        "intent_labels": intent_label,
        "version": int(spec.get("version") or 1),
        "sha": str(spec.get("_sha") or ""),
        "source_path": str(spec.get("_source_path") or ""),
        "custom": {
            "skill_drivers": bool(bindings.get("skill_drivers")),
            "role_skills": bool(bindings.get("role_skills")),
            "trigger_roles": bool(bindings.get("trigger_roles")),
            "trigger_skills": bool(bindings.get("trigger_skills")),
        },
    }
