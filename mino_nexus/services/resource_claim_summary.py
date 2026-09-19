"""Claim 人类可读摘要（导入预览 / QA 流程）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.services.resource_preflight import claim_requires_clear_cache


def resource_claim_summary(
    claim: dict[str, Any] | None,
    *,
    scene: dict[str, Any] | None = None,
    precondition: str = "",
) -> str:
    if not isinstance(claim, dict) or not claim:
        return ""
    parts: list[str] = []
    plat = str(claim.get("platform") or "").strip()
    if plat:
        parts.append(f"端={plat}")
    da = claim.get("device_app") if isinstance(claim.get("device_app"), dict) else {}
    rs = str(da.get("required_session") or "").strip()
    if rs:
        parts.append(f"机态={rs}")
    if claim_requires_clear_cache(claim, scene, precondition):
        parts.append("须清缓存")
    acc = claim.get("account") if isinstance(claim.get("account"), dict) else {}
    req = acc.get("requirements") if isinstance(acc.get("requirements"), dict) else {}
    n = len(req.get("all") or [])
    if n:
        parts.append(f"选号约束×{n}")
    pkg = ""
    ta = claim.get("target_app") if isinstance(claim.get("target_app"), dict) else {}
    pkg = str(ta.get("package") or "").strip()
    if pkg:
        parts.append(f"包={pkg[:24]}")
    return " · ".join(parts)
