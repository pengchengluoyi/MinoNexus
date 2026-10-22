"""按应用配置的展示名/别名治理（已停用：规则改由 Studio 手工编辑 + atlas 聚类命名）。"""
from __future__ import annotations

from typing import Any

APP_GOVERNANCE: dict[str, list[dict[str, Any]]] = {}


def apply_governance_to_doc(app_id: str, doc: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """兼容旧 API；不再改写 doc。"""
    return doc, 0


def upgrade_app_alias_governance(app_id: str | None = None) -> int:
    return 0
