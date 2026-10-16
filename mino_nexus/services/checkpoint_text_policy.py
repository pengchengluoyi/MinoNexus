"""文案匹配合同；作者指定文案与跨语言意图分别验证。旧快照不重写。"""
from __future__ import annotations

import re
from typing import Any

TEXT_MODES = frozenset({"copy", "language_intent"})
_QUOTED = re.compile(r"[「『“\"]([^」』”\"\n]+)[」』”\"]")
_FIXED = re.compile(r"(?:标题|正文|按钮(?:文字|文案)|文案)(?:必须)?(?:为|是)|逐字|一字不差|精确(?:匹配|文案)|固定文案|指定文案|exact\s+(?:text|wording|match)", re.I)
_INTENT = re.compile(r"不要求逐字|不限定文案|非指定文案|(?:语义|意图|含义)一致|semantic\s+(?:match|equivalence)", re.I)


def compiled_text_rules(mode: str, rules: dict[str, Any], source: str) -> dict[str, Any]:
    """只在新编译时选择默认策略；不能因升级改写历史 exact 检查点。"""
    out = dict(rules)
    if mode not in TEXT_MODES:
        return out
    literal = bool(_QUOTED.search(source)) and not _INTENT.search(source)
    fixed = bool(_FIXED.search(source)) and not _INTENT.search(source)
    if mode == "copy":
        exact = literal or fixed
        out["text_match"] = "exact" if exact else "intent"
        out["text_policy_source"] = "author_literal" if exact else "default_intent"
    else:
        out["text_match"] = "exact" if literal or fixed else "intent"
        out["text_policy_source"] = "author_literal" if literal or fixed else "language_intent"
    if out["text_match"] == "exact":
        out.setdefault("match", "contains" if out.get("body") else "equals")
    return out


def text_policy(checkpoint: dict[str, Any]) -> str:
    mode = str(checkpoint.get("checkpoint_kind") or "")
    if mode not in TEXT_MODES:
        return ""
    rules = checkpoint.get("rules") if isinstance(checkpoint.get("rules"), dict) else {}
    value = str(rules.get("text_match") or "")
    if value in {"exact", "intent"}:
        return value
    return "intent" if mode == "language_intent" else "exact"


def expected_text(checkpoint: dict[str, Any]) -> str:
    rules = checkpoint.get("rules") if isinstance(checkpoint.get("rules"), dict) else {}
    return str(rules.get("meaning") or rules.get("title") or rules.get("body")
               or rules.get("value") or rules.get("label") or "").strip()


def language_matches(expected: str, observed: str) -> bool:
    aliases = {"中文": "zh", "汉语": "zh", "英文": "en", "英语": "en", "日语": "ja", "韩语": "ko"}
    def tag(value: str) -> str:
        value = value.strip().lower().replace("_", "-")
        return aliases.get(value, value)
    want, actual = tag(expected), tag(observed)
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", actual) or actual in {"unknown", "und", "mixed"}:
        return False
    return not want or actual == want or actual.startswith(want + "-")
