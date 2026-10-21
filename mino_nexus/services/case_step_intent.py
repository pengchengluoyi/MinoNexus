"""编号步意图角色：程序校验 intent_role 与 key_ref 一致性，不做产品文案关键字匹配。"""
from __future__ import annotations

from typing import Any

# 模型在 case-step-compile 输出；仅机器枚举，不对应作者句式表。
INTENT_ROLES = frozenset(
    {
        "read_only",
        "observe_ui",
        "navigate_to_screen",
        "system_app_lifecycle",
        "mutate_ui",
        "assert_absent",
        "assert_screen_endstate",
        "assert_human_only",
        "assert_copy",
        "assert_other",
    }
)

_OPERATION_INTENT_DEFAULT_REF: dict[str, str] = {
    "read_only": "operation.author_remark",
    "observe_ui": "operation.observe",
    "navigate_to_screen": "operation.nav_target",
    "system_app_lifecycle": "operation.system_app",
}

_EXPECTED_INTENT_DEFAULT_REF: dict[str, str] = {
    "assert_absent": "expected.ui_absent",
    "assert_screen_endstate": "expected.screen_reached",
    "assert_human_only": "expected.human_review",
    "assert_copy": "expected.ui_copy",
}

_LIFECYCLE_HOOKS = frozenset({"close_app", "kill_app", "launch_app", "open_app"})


def normalize_intent_role(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in INTENT_ROLES else ""


def canonical_key_ref(intent_role: str, layer: str) -> str:
    role = normalize_intent_role(intent_role)
    if not role:
        return ""
    if layer == "operation":
        return _OPERATION_INTENT_DEFAULT_REF.get(role, "")
    if layer == "expected":
        return _EXPECTED_INTENT_DEFAULT_REF.get(role, "")
    return ""


def apply_span_intent(span: dict[str, Any], layer: str) -> dict[str, Any]:
    """按 intent_role 纠正 key_ref（闭集映射，不读原文关键词）。"""
    out = dict(span)
    role = normalize_intent_role(out.get("intent_role"))
    if not role:
        return out
    want = canonical_key_ref(role, layer)
    if want:
        out["key_ref"] = want
    return out


def reconcile_hit_layer(hit: dict[str, Any], *, author_layer: str) -> dict[str, Any]:
    """操作/预期栏与命中层不一致时，按 intent_role 仲裁，避免误报 layer_mismatch。"""
    row = dict(hit)
    role = normalize_intent_role(row.get("intent_role"))
    ref = str(row.get("key_ref") or "")
    hit_layer = str(row.get("layer") or "")
    author = str(author_layer or "")

    if author == "operation" and role == "read_only":
        row.update(layer="operation", key_ref="operation.author_remark", kind="remark")
        return row

    if author == "operation" and role in ("navigate_to_screen", ""):
        if role == "navigate_to_screen" or ref == "expected.screen_reached":
            row.update(layer="operation", key_ref="operation.nav_target", kind="nav")
            target = row.get("target") if isinstance(row.get("target"), dict) else {}
            rules = row.get("rules") if isinstance(row.get("rules"), dict) else {}
            nav = str(rules.get("nav_target") or target.get("name") or "").strip()
            if nav:
                row["nav_target"] = nav
            return row

    if author == "operation" and role == "system_app_lifecycle":
        rules = dict(row.get("rules") or {})
        hook = str(rules.get("hook_cap") or row.get("hook_cap") or "kill_app").strip()
        if hook not in _LIFECYCLE_HOOKS:
            hook = "kill_app"
        row.update(
            layer="operation",
            key_ref="operation.system_app",
            kind="hook",
            hook_cap=hook,
            rules={**rules, "hook_cap": hook},
        )
        return row

    if author == "operation" and ref.startswith("expected.") and role == "read_only":
        row.update(layer="operation", key_ref="operation.author_remark", kind="remark")
        return row

    if author == "expected" and role == "assert_human_only":
        row.update(layer="expected", key_ref="expected.human_review", assert_mode="human_only")
        return row

    if author == "expected" and role == "assert_absent":
        row.update(layer="expected", key_ref="expected.ui_absent", assert_mode="ui_absent")
        return row

    if hit_layer and author and hit_layer != author:
        # 仍跨层且无意图：保留原 hit，由 _classify_block 报 layer_mismatch
        return row
    row["layer"] = author or hit_layer
    return row


def intent_role_errors(span: dict[str, Any], spec: dict[str, Any], layer: str) -> list[str]:
    """intent_role 与所选 key_ref / hook 的结构一致性。"""
    role = normalize_intent_role(span.get("intent_role"))
    ref = str(spec.get("key_ref") or "")
    errors: list[str] = []
    if layer == "operation" and ref == "operation.author_remark":
        from mino_nexus.services.case_semantic_integrity import explicit_remark
        if not explicit_remark(span.get("quote")):
            errors.append("只有原文明示备注或不执行的说明才可跳过；必需操作和观察必须选择可执行事件，目录不支持时保留未覆盖，不得改为说明")
    if not role:
        return errors
    want = canonical_key_ref(role, layer)
    if want and ref != want:
        errors.append(f"intent_role={role} 应使用 {want}，当前为 {ref or '空'}")
    if role == "system_app_lifecycle" and layer == "operation":
        params = span.get("params") if isinstance(span.get("params"), dict) else {}
        hook = str(params.get("hook_cap") or "").strip()
        if hook and hook not in _LIFECYCLE_HOOKS:
            errors.append(f"hook_cap {hook} 不是应用生命周期能力")
        if ref == "operation.tap" or str(spec.get("hook_cap") or "") == "tap_element":
            errors.append("应用生命周期意图不得映射为点击事件")
    if role == "read_only" and layer == "operation" and ref not in ("operation.author_remark", ""):
        if want:
            errors.append("read_only 意图应使用 operation.author_remark")
    return errors


def blocked_severity(reason: str, *, key_ref: str = "") -> str:
    """blocking 拉低 coverable；human_only / advisory 仅展示。"""
    if reason in ("human_only", "advisory"):
        return reason
    if key_ref == "expected.human_review":
        return "human_only"
    return "blocking"
