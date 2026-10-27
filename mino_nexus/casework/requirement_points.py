"""需求点 id：覆盖度主分母，避免每条用例都落成 case:<id> 兜底。"""
from __future__ import annotations

import hashlib
import re
from typing import Any


def _slug(text: str) -> str:
    raw = re.sub(r"\s+", " ", str(text or "").strip())
    if not raw:
        return ""
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    head = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", raw[:32]).strip("-")[:32]
    return (head or "rp") + "-" + digest


def _intent_ids(case: dict[str, Any]) -> list[str]:
    meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
    summary = meta.get("intent_summary") if isinstance(meta.get("intent_summary"), dict) else {}
    ids = [str(x) for x in (summary.get("intents") or []) if str(x).strip()]
    if ids:
        return ids
    out: list[str] = []
    for bundle in (meta.get("step_program_keys") or {}).values():
        if not isinstance(bundle, dict):
            continue
        for iid in bundle.get("intent_ids") or []:
            s = str(iid).strip()
            if s and s not in out:
                out.append(s)
    return out


def requirement_point_ids(case: dict[str, Any]) -> tuple[list[str], str]:
    """返回 (point_ids, source)。source: explicit | module | intent | fallback。"""
    if not isinstance(case, dict):
        return [], "fallback"
    explicit = [str(x).strip() for x in (case.get("point_ids") or []) if str(x).strip()]
    if explicit:
        return list(dict.fromkeys(explicit)), "explicit"
    module = str(case.get("module") or "").strip()
    name = str(case.get("name") or "").strip()
    if module and name:
        return [f"rp:{module}:{_slug(name)}"], "module"
    intents = _intent_ids(case)
    if intents:
        return [f"intent:{iid}" for iid in intents[:5]], "intent"
    cid = str(case.get("case_id") or "").strip()
    if cid:
        return [f"case:{cid}"], "fallback"
    return [], "fallback"


def ensure_point_ids(case: dict[str, Any]) -> tuple[list[str], str]:
    """编译/同步时写入 case.point_ids（不覆盖已有 explicit）。"""
    ids, source = requirement_point_ids(case)
    if source != "explicit" and ids:
        case["point_ids"] = ids
    return ids, source
