"""已落库程序图的预览行：纯格式化，不调模型。"""
from __future__ import annotations

from typing import Any

def _format_plan_line(step: dict[str, Any], *, case_step: int) -> str:
    ref = str(step.get("key_ref") or "").strip()
    if ref == "operation.author_remark" or str(step.get("kind") or "") == "remark":
        title = str(step.get("title") or step.get("source_clause") or "").strip()
        return f"{int(case_step)}. 说明步骤（执行跳过）{title}"[:240]
    if ref == "expected.human_review":
        title = str(step.get("title") or step.get("source_clause") or "").strip()
        return f"{int(case_step)}. 人工复核 {title}"[:240]
    slots = step.get("slots") if isinstance(step.get("slots"), list) else []
    if ref == "operation.login_flow" or (str(step.get("kind") or "") == "flow_block" and slots):
        parts: list[str] = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            label = _SLOT_ZH.get(str(slot.get("id") or ""), str(slot.get("id") or "槽"))
            text = str(slot.get("text") or "").strip()
            target = slot.get("target") if isinstance(slot.get("target"), dict) else {}
            role = _ROLE_ZH.get(str(target.get("role") or ""), str(target.get("role") or ""))
            name = str(target.get("name") or "").strip()
            extra = f" · {role}" if role else ""
            if name and name not in text:
                extra = f"{extra} {name}".rstrip()
            parts.append(f"{label}「{text}」{extra}" if text else label)
        body = "；".join(parts) if parts else "登录流"
        return f"{int(case_step)}. 登录流 {body}"[:240]
    title = str(step.get("title") or step.get("source_clause") or step.get("source_line") or "").strip()
    rules = {}
    assert_payload = step.get("assert") if isinstance(step.get("assert"), dict) else {}
    params = step.get("params") if isinstance(step.get("params"), dict) else {}
    if isinstance(assert_payload.get("rules"), dict):
        rules = assert_payload["rules"]
    elif isinstance(params.get("rules"), dict):
        rules = params["rules"]
    element = assert_payload.get("element") if isinstance(assert_payload.get("element"), dict) else {}
    if not element and isinstance(params.get("element"), dict):
        element = params["element"]
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    event = title or ref or "（空）"
    role = _ROLE_ZH.get(str(target.get("role") or element.get("role") or ""), str(target.get("role") or element.get("role") or ""))
    name = str(target.get("name") or rules.get("name") or rules.get("target") or "").strip()
    if role and role not in event:
        event = f"{event} · {role}"
    if name and name not in event:
        event = f"{event} {name}"
    shown = _rules_zh(rules)
    if shown and shown not in event:
        event = f"{event}（{shown}）"
    return f"{int(case_step)}. {event}"[:240]


def _format_block_line(item: dict[str, Any], *, case_step: int) -> str:
    message = str(item.get("user_message") or "").strip()
    if message:
        return message[:240]
    clause = str(item.get("source_clause") or item.get("line") or item.get("raw_text") or "").strip()
    reason = _block_reason_zh(str(item.get("reason") or "unmapped"))
    return f"{int(case_step)}. × {clause}（{reason}）"[:240]





def preview_lines_from_plans(spk: dict[str, Any]) -> tuple[list[str], list[str]]:
    """只读已经落库的程序图，不调用模型。"""
    steps_parsed: list[str] = []
    expected_parsed: list[str] = []

    def _step_num(key: str) -> int:
        try:
            return int(key)
        except (TypeError, ValueError):
            return 0

    for key in sorted(spk.keys(), key=_step_num):
        bundle = spk.get(key) if isinstance(spk.get(key), dict) else {}
        n = _step_num(str(key)) or 1
        do_plan = bundle.get("do_program_plan") if isinstance(bundle.get("do_program_plan"), dict) else {}
        ck_plan = bundle.get("check_program_plan") if isinstance(bundle.get("check_program_plan"), dict) else {}
        for step in do_plan.get("steps") or []:
            if isinstance(step, dict):
                steps_parsed.append(_format_plan_line(step, case_step=n))
        for warning in do_plan.get("compile_warnings") or []:
            blocked = warning.get("blocked") if isinstance(warning, dict) and isinstance(warning.get("blocked"), dict) else None
            if blocked:
                steps_parsed.append(_format_block_line(blocked, case_step=n))
        for step in ck_plan.get("checkpoints") or []:
            if isinstance(step, dict):
                expected_parsed.append(_format_plan_line(step, case_step=n))
        for warning in ck_plan.get("compile_warnings") or []:
            blocked = warning.get("blocked") if isinstance(warning, dict) and isinstance(warning.get("blocked"), dict) else None
            if blocked:
                expected_parsed.append(_format_block_line(blocked, case_step=n))
    return steps_parsed[:40], expected_parsed[:40]


def stored_preview_lines(case: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    if not isinstance(case, dict):
        return [], []
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    spk = meta.get("step_program_keys") if isinstance(meta.get("step_program_keys"), dict) else {}
    if not spk:
        return [], []
    return preview_lines_from_plans(spk)


_SLOT_ZH = {"credential": "凭证", "submit": "提交", "entry": "入口"}
_ROLE_ZH = {
    "button": "按钮",
    "element": "元素",
    "image": "图片",
    "checkbox": "勾选框",
    "switch": "开关",
    "input": "输入框",
    "page": "页面",
    "dialog": "弹窗",
}
def _block_reason_zh(reason: str) -> str:
    raw = str(reason or "").strip()
    return _BLOCK_REASON_ZH.get(raw, raw or "无法覆盖")

def _rules_zh(rules: dict[str, Any]) -> str:
    parts: list[str] = []
    if rules.get("present"):
        parts.append("弹出")
    if rules.get("exclusive"):
        parts.append("不展示其他弹窗")
    which = str(rules.get("which") or "").strip()
    if which:
        parts.append(which)
    body = str(rules.get("body") or rules.get("text") or rules.get("title") or "").strip()
    if body:
        parts.append(f"「{body}」")
    name = str(rules.get("name") or rules.get("label") or "").strip()
    if name and name not in " ".join(parts):
        parts.append(name)
    return "，".join(parts)


_BLOCK_REASON_ZH = {
    "unmapped": "未映射到事件",
    "uncoverable_event": "当前不能做",
    "unavailable_cap": "事件已下线",
    "fixture_required": "需准备业务数据",
    "manual_prerequisite_required": "需人工准备",
    "ui_state_precondition": "需确认起点界面",
    "device_state_setup_required": "需准备设备状态",
    "device_session_setup_required": "需准备登录态",
    "precondition_is_action": "前置里写入了操作",
    "precondition_coverage_missing": "前置未编译",
    "precondition_scene_mismatch": "前置与场景不一致",
    "ui_partial": "仅部分可执行",
    "required_operation_skipped": "必需操作被跳过",
}
