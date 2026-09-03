"""四层编排：驱动 → 技能 → 角色 → 触发。绑定可覆盖，目录本身写死。"""
from __future__ import annotations

from typing import Any

from mino_nexus import settings_store as ss

DRIVERS: list[dict[str, Any]] = [
    {"id": "adb", "label": "真机 ADB", "kind": "device", "summary": "本机点按、截图、装包"},
    {"id": "claw", "label": "Scout 远程", "kind": "device", "summary": "经 Scout 执行"},
    {"id": "im.send", "label": "IM 发消息", "kind": "channel", "summary": "通道回消息"},
    {"id": "figma.sync", "label": "Figma 拉稿", "kind": "plugin", "summary": "按应用拉设计稿"},
]

SKILL_CATEGORIES: list[dict[str, str]] = [
    {"id": "flow", "label": "流程产出", "desc": "读需求、写脑图和用例"},
    {"id": "device", "label": "设备操作", "desc": "真机上规划、点按、定位和断言"},
    {"id": "channel", "label": "通道对话", "desc": "IM 里回答、下令"},
    {"id": "sync", "label": "外部同步", "desc": "设计稿 / 文档"},
]

SKILLS: list[dict[str, Any]] = [
    {"id": "im.dialogue", "label": "IM 对话", "owner": "im-qa-assistant", "summary": "在通道里回答和下令", "intent": "talk", "category": "channel"},
    {"id": "im.defect", "label": "IM 提缺陷", "owner": "im-defect-assistant", "summary": "把说清的缺陷整理成单", "intent": "act", "category": "channel"},
    {"id": "analyze_req", "label": "拆验收标准", "owner": "req-analyst", "summary": "读原文，拆测试点", "intent": "persist", "category": "flow"},
    {"id": "propose_atlas", "label": "建议图谱", "owner": "req-analyst", "summary": "出品变更等人确认", "intent": "persist", "category": "flow"},
    {"id": "draft_mindmap", "label": "写测试脑图", "owner": "mindmap-writer", "summary": "按入口和端铺脑图", "intent": "persist", "category": "flow"},
    {"id": "draft_cases", "label": "写用例草稿", "owner": "case-writer", "summary": "按测试点出步骤", "intent": "persist", "category": "flow"},
    {"id": "map_cases", "label": "对照用例库", "owner": "req-qa-bm", "summary": "看覆盖够不够", "intent": "persist", "category": "flow"},
    {"id": "draft_sign", "label": "验收草稿", "owner": "req-qa-bm", "summary": "出建议，结论人点", "intent": "persist", "category": "flow"},
    {"id": "pick_regression", "label": "圈回归范围", "owner": "version-qa-bm", "summary": "圈本版回归用例", "intent": "persist", "category": "flow"},
    {"id": "draft_gate", "label": "发版草稿", "owner": "version-qa-bm", "summary": "出建议，结论人点", "intent": "persist", "category": "flow"},
    {"id": "pick_device", "label": "申请执行设备", "owner": "test-engineer", "summary": "按用例占用当前环境设备", "intent": "persist", "category": "flow"},
    {"id": "agent-decide", "label": "看图决策", "owner": "test-engineer", "summary": "每一步决定下一个动作", "intent": "act", "category": "device"},
    {"id": "plan-overview", "label": "规划步骤", "owner": "test-engineer", "summary": "Plan 模式先排事件", "intent": "act", "category": "device"},
    {"id": "assert-vision", "label": "视觉断言", "owner": "test-engineer", "summary": "检查点是否达成", "intent": "act", "category": "device"},
]

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
    "im.dialogue": ["im.send"],
    "agent-decide": ["adb", "claw"],
    "plan-overview": ["adb", "claw"],
    "assert-vision": ["adb", "claw"],
}

DEFAULT_ROLE_SKILLS: dict[str, list[str]] = {
    "im-qa-assistant": ["im.dialogue"],
    "im-defect-assistant": ["im.defect"],
    "req-analyst": ["analyze_req", "propose_atlas"],
    "mindmap-writer": ["draft_mindmap"],
    "case-writer": ["draft_cases"],
    "req-qa-bm": ["map_cases", "draft_sign"],
    "version-qa-bm": ["pick_regression", "draft_gate"],
    "test-engineer": ["pick_device", "agent-decide", "plan-overview", "assert-vision"],
    "conductor": [],
}

DEFAULT_TRIGGER_ROLES: dict[str, dict[str, str]] = {
    "im_chat": {"dialogue": "im-qa-assistant", "defect": "im-defect-assistant"},
    "qa_tick": {"default": "conductor"},
    "case_run": {"default": "test-engineer"},
    "settings_chat": {"default": ""},
}

DEFAULT_TRIGGER_SKILLS: dict[str, dict[str, str]] = {
    "im_chat": {"dialogue": "im.dialogue", "defect": "im.defect"},
    "qa_tick": {"default": ""},
    "case_run": {"default": "agent-decide"},
    "settings_chat": {"default": ""},
}

_INTENT_LABEL = {"dialogue": "问答", "defect": "提缺陷", "default": "默认"}
_SKILL_IDS = {row["id"] for row in SKILLS}
_ROLE_IDS = {row["id"] for row in ROLES}


def _store() -> dict[str, Any]:
    raw = ss.get_layer_stack()
    return raw if isinstance(raw, dict) else {}


def _clean_id_list(raw: Any, allowed: set[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        sid = str(item or "").strip()
        if not sid or sid in seen or sid not in allowed:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _merged_role_skills() -> dict[str, list[str]]:
    extra = _store().get("role_skills")
    extra = extra if isinstance(extra, dict) else {}
    out = {rid: list(ids) for rid, ids in DEFAULT_ROLE_SKILLS.items()}
    for rid in _ROLE_IDS:
        if rid in extra:
            out[rid] = _clean_id_list(extra[rid], _SKILL_IDS)
    return out


def get_stack() -> dict[str, Any]:
    role_skills = _merged_role_skills()
    extra_sd = _store().get("skill_drivers")
    extra_sd = extra_sd if isinstance(extra_sd, dict) else {}
    skill_drivers = {**DEFAULT_SKILL_DRIVERS, **{
        sid: _clean_id_list(ids, {d["id"] for d in DRIVERS})
        for sid, ids in extra_sd.items() if sid in _SKILL_IDS
    }}
    extra_tr = _store().get("trigger_roles")
    extra_tr = extra_tr if isinstance(extra_tr, dict) else {}
    extra_ts = _store().get("trigger_skills")
    extra_ts = extra_ts if isinstance(extra_ts, dict) else {}
    trigger_roles = {**DEFAULT_TRIGGER_ROLES, **extra_tr}
    trigger_skills = {**DEFAULT_TRIGGER_SKILLS, **extra_ts}

    skills = []
    for row in SKILLS:
        sid = row["id"]
        skills.append({
            **row,
            "driver_ids": list(skill_drivers.get(sid) or []),
        })
    roles = []
    for row in ROLES:
        rid = row["id"]
        ids = list(role_skills.get(rid) or [])
        roles.append({
            **row,
            "skill_ids": ids,
            "skills": [s for s in skills if s["id"] in ids],
        })
    triggers = []
    for row in TRIGGERS:
        tid = row["id"]
        triggers.append({
            **row,
            "roles": trigger_roles.get(tid) or {},
            "skills": trigger_skills.get(tid) or {},
            "intent_labels": {key: _INTENT_LABEL.get(key, key) for key in (row.get("intents") or ["default"])},
        })
    return {
        "drivers": list(DRIVERS),
        "skill_categories": list(SKILL_CATEGORIES),
        "skills": skills,
        "roles": roles,
        "triggers": triggers,
        "skill_drivers": skill_drivers,
        "role_skills": role_skills,
        "trigger_roles": trigger_roles,
        "trigger_skills": trigger_skills,
        "intent_labels": dict(_INTENT_LABEL),
        "custom": {
            "skill_drivers": bool(_store().get("skill_drivers")),
            "role_skills": bool(_store().get("role_skills")),
            "trigger_roles": bool(_store().get("trigger_roles")),
            "trigger_skills": bool(_store().get("trigger_skills")),
        },
    }


def save_bindings(body: dict[str, Any] | None, *, reset: bool = False) -> dict[str, Any]:
    if reset:
        ss.save_layer_stack({}, reset=True)
        return get_stack()
    incoming = body if isinstance(body, dict) else {}
    payload: dict[str, Any] = {}
    if "skill_drivers" in incoming and isinstance(incoming["skill_drivers"], dict):
        payload["skill_drivers"] = {
            sid: _clean_id_list(ids, {d["id"] for d in DRIVERS})
            for sid, ids in incoming["skill_drivers"].items()
            if str(sid) in _SKILL_IDS
        }
    if "role_skills" in incoming and isinstance(incoming["role_skills"], dict):
        payload["role_skills"] = {
            rid: _clean_id_list(ids, _SKILL_IDS)
            for rid, ids in incoming["role_skills"].items()
            if str(rid) in _ROLE_IDS
        }
    if "trigger_roles" in incoming:
        payload["trigger_roles"] = incoming["trigger_roles"]
    if "trigger_skills" in incoming:
        payload["trigger_skills"] = incoming["trigger_skills"]
    if payload:
        ss.save_layer_stack(payload, reset=False)
    return get_stack()
