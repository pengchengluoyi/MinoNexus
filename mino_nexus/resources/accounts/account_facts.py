"""账号事实词汇与用例侧编译。

账号事实和「流程阶段」不是一类东西：
- registered_days 是账号在应用内完成注册后的天数，可做 ``lte 30`` / ``gt 30``；
- register_flow 是注册流程走到了哪一步，不能拿来表达账号年龄。

这里只定义稳定字段与自然语言抽取；是否允许某条事实作为前置，由
``case_semantic_integrity.validate_precondition_hits`` 再校验。
"""
from __future__ import annotations

import re
from typing import Any

FACT_VALUE_KIND_ENUM = "enum"
FACT_VALUE_KIND_NUMBER = "number"
FACT_VALUE_KINDS = frozenset({FACT_VALUE_KIND_ENUM, FACT_VALUE_KIND_NUMBER})

NUMERIC_OPS = frozenset({"lt", "lte", "gt", "gte"})
CLAUSE_OPS = frozenset({"eq", "ne", "in"}) | NUMERIC_OPS

# 内置账号事实。它们不属于某个业务模板，任何项目都能用。
ACCOUNT_FACT_FIELDS: list[dict[str, Any]] = [
    {
        "key": "registered_days",
        "label": "注册天数",
        "help": "账号在应用内完成注册后的天数；0 表示刚注册。用于「注册≤30天」「注册超过30天」等事实约束。",
        "data_kind": "static",
        "value_kind": FACT_VALUE_KIND_NUMBER,
        "unit": "天",
        "min": 0,
        "max": 36500,
        "options": [{"value": "unknown", "label": "未设置"}],
        "source": "builtin",
    },
]

_PREFIX = r"注册(?:时间|时长|天数|日期)?\s*"
# 顺序很重要：先匹配「不超过」再匹配「超过」，先匹配「未满」再匹配「满」。
_AGE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(_PREFIX + r"(?:≤|<=|不超过|未超过|至多)\s*(\d+)\s*天"), "lte"),
    (re.compile(_PREFIX + r"(?:少于|小于|不足|未满|不满|不到|<)\s*(\d+)\s*天"), "lt"),
    (re.compile(_PREFIX + r"(?:超过|大于|多于|超|>)\s*(\d+)\s*天"), "gt"),
    (re.compile(_PREFIX + r"(?:≥|>=|满|达到)\s*(\d+)\s*天"), "gte"),
    (re.compile(_PREFIX + r"(\d+)\s*天(?:以内|内|以下)"), "lte"),
    (re.compile(_PREFIX + r"(\d+)\s*天(?:以上)"), "gte"),
)


def registration_age_clauses(text: str) -> list[dict[str, str]]:
    """从一段前置/步骤里抽取注册天数事实；抽不到返回空。"""
    blob = str(text or "")
    if not blob or "注册" not in blob:
        return []
    for pattern, op in _AGE_PATTERNS:
        found = pattern.search(blob)
        if not found:
            continue
        value = str(int(found.group(1)))
        return [{"facet": "registered_days", "op": op, "value": value}]
    return []


def account_fact_field(key: str) -> dict[str, Any] | None:
    for row in ACCOUNT_FACT_FIELDS:
        if str(row.get("key") or "") == str(key or ""):
            return row
    return None


def is_numeric_fact(definition: dict[str, Any] | None) -> bool:
    return str((definition or {}).get("value_kind") or "").strip().lower() == FACT_VALUE_KIND_NUMBER


def parse_number(value: Any) -> float | None:
    text = str(value or "").strip().lower()
    if not text or text == "unknown":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def normalize_fact_value(definition: dict[str, Any] | None, value: Any) -> str:
    """把数值 fact 规范成不带小数点尾巴的字符串；枚举 fact 返回原值。"""
    raw = str(value or "").strip().lower()
    if not is_numeric_fact(definition):
        return raw
    number = parse_number(raw)
    if number is None:
        return "unknown"
    low = float(definition.get("min") if definition.get("min") is not None else 0)
    high = float(definition.get("max") if definition.get("max") is not None else 36500)
    if number < low or number > high:
        return "unknown"
    return str(int(number)) if number.is_integer() else str(number)


def fact_value_configured(definition: dict[str, Any] | None, value: Any) -> bool:
    raw = str(value or "").strip().lower()
    if not raw or raw == "unknown":
        return False
    if is_numeric_fact(definition):
        return parse_number(raw) is not None and normalize_fact_value(definition, raw) != "unknown"
    options = {
        str(option.get("value") or "").strip().lower()
        for option in (definition or {}).get("options") or []
        if isinstance(option, dict)
    }
    return raw in options
