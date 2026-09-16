"""按应用配置的展示名/别名治理（Console/Studio 可复用同一规则，非聚类硬编码）。"""
from __future__ import annotations

from typing import Any

# 造物相机
ZAOWU_CAMERA_APP_ID = "3d2b9799-0027-4c7d-bfe7-8c5b88f4087d"


def _norm(s: str) -> str:
    return str(s or "").strip()


def _labels(meta: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for key in ("display_name", "header_title", "page_title"):
        v = _norm(meta.get(key) or "")
        if v:
            out.add(v)
    for a in meta.get("aliases") or []:
        v = _norm(a)
        if v:
            out.add(v)
    for a in meta.get("name_samples") or []:
        v = _norm(a)
        if v:
            out.add(v)
    return out


def _rule_match(meta: dict[str, Any], needles: list[str]) -> bool:
    hay = _labels(meta)
    for n in needles:
        if not n:
            continue
        if any(n in h or h in n for h in hay):
            return True
    return False


GovernanceRule = dict[str, Any]

APP_GOVERNANCE: dict[str, list[GovernanceRule]] = {
    ZAOWU_CAMERA_APP_ID: [
        {
            "needles": ["内容均由AI生成", "均由AI生成"],
            "display_name": "潮玩详情",
            "aliases_add": ["内容均由AI生成", "AI生成说明"],
            "stop_after_match": True,
        },
        {
            "needles": ["造物结果", "生成完成"],
            "display_name": "造物结果",
            "aliases_add": ["造物完成页", "生成完成"],
            "stop_after_match": True,
        },
        {
            "needles": ["用户信息区", "用户信息区域"],
            "display_name": "我的",
            "aliases_add": ["我的", "我的页面", "个人中心", "我"],
            "stop_after_match": True,
        },
        {
            "needles": ["我的"],
            "display_name": "我的",
            "aliases_add": ["个人中心", "我", "我的页面"],
        },
        {
            "needles": ["首页", "发现"],
            "display_name": "首页",
            "aliases_add": ["发现", "推荐"],
        },
    ],
}


def apply_governance_to_doc(app_id: str, doc: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """合并规则到 doc.states[].meta；返回 (doc, 修改 state 数)。"""
    rules = APP_GOVERNANCE.get(str(app_id or "").strip()) or []
    if not rules or not doc:
        return doc, 0
    changed = 0
    states = []
    for st in doc.get("states") or []:
        if not isinstance(st, dict):
            continue
        row = dict(st)
        meta = dict(row.get("meta") or {})
        touched = False
        for rule in rules:
            if not _rule_match(meta, list(rule.get("needles") or [])):
                continue
            dn = _norm(rule.get("display_name") or "")
            if dn and meta.get("display_name") != dn:
                meta["display_name"] = dn
                touched = True
            aliases = [_norm(a) for a in (meta.get("aliases") or []) if _norm(a)]
            for a in rule.get("aliases_add") or []:
                val = _norm(a)
                if val and val not in aliases:
                    aliases.append(val)
                    touched = True
            if touched:
                meta["aliases"] = aliases[:16]
            if rule.get("stop_after_match"):
                break
        if touched:
            changed += 1
        row["meta"] = meta
        states.append(row)
    out = dict(doc)
    out["states"] = states
    return out, changed


def upgrade_app_alias_governance(app_id: str | None = None) -> int:
    """启动时写入 draft NavFSM（仅当有规则且能读到 draft）。"""
    from mino_nexus.services import nav_fsm_store as store

    ids = [app_id] if app_id else list(APP_GOVERNANCE.keys())
    total = 0
    for aid in ids:
        if not aid:
            continue
        doc = store.read_raw(aid, version=store.DRAFT_VERSION)
        if not doc:
            doc = store.read_raw(aid, version=store.DEFAULT_VERSION)
        if not doc:
            continue
        merged, n = apply_governance_to_doc(aid, doc)
        if n <= 0:
            continue
        store.save_draft(aid, merged, updated_by="alias_governance")
        pub = store.read_raw(aid, version=store.DEFAULT_VERSION)
        if pub:
            pub_merged, pub_n = apply_governance_to_doc(aid, pub)
            if pub_n > 0:
                store.save(aid, pub_merged, updated_by="alias_governance")
        total += n
    return total
