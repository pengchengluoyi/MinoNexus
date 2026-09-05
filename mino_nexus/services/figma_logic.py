"""把 Figma 页面结构写入应用 playbook / 图谱。不接 CLIP、不抽图标像素。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mino_nexus.services import app_automation as aas
from mino_nexus.services import figma_service as fs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def apply_figma_logic(app: dict[str, Any], *, file_url: str = "", file_key: str = "", token: str = "", write_knowledge: bool = True, write_graph: bool = True) -> dict[str, Any]:
    synced = fs.sync_figma_file(
        file_url=file_url,
        file_key=file_key,
        depth=8,
        token=token or None,
        include_raw_document=False,
    )
    cfg = aas.save_automation_config(app, {"figma": {k: v for k, v in synced.items() if k != "raw_document"}})
    pages = ((synced.get("logic") or {}).get("pages") or []) if isinstance(synced.get("logic"), dict) else []
    playbook = aas.get_playbook(app)
    playbook = dict(playbook or {})
    if write_knowledge:
        playbook["figma_pages"] = [
            {"name": p.get("name"), "keywords": p.get("keywords") or [], "texts": (p.get("texts") or [])[:12]}
            for p in pages if isinstance(p, dict)
        ]
        playbook["figma_applied_at"] = _now()
        aas.save_playbook(app, playbook)
    atlas_modules = []
    if write_graph:
        for p in pages:
            if not isinstance(p, dict):
                continue
            atlas_modules.append({
                "id": p.get("node_id") or "",
                "name": p.get("name") or "页面",
                "summary": "、".join((p.get("keywords") or [])[:6]),
                "source": "figma",
                "features": [{"id": "", "name": f.get("name"), "summary": ""} for f in (p.get("frames") or [])[:12] if isinstance(f, dict)],
            })
        qp = dict((cfg.get("qa_process") or {}))
        atlas = qp.get("app_atlas") if isinstance(qp.get("app_atlas"), dict) else {"modules": []}
        atlas = dict(atlas)
        atlas["modules"] = atlas_modules
        atlas["updated_at"] = _now()
        atlas["source"] = "figma"
        qp["app_atlas"] = atlas
        cfg = aas.save_automation_config(app, {"qa_process": qp, "figma": cfg.get("figma")})
    return {
        "figma": cfg.get("figma") or synced,
        "page_count": synced.get("page_count", 0),
        "frame_count": synced.get("frame_count", 0),
        "logic_pages": len(pages),
        "atlas_modules": len(atlas_modules),
        "playbook_pages": len(playbook.get("figma_pages") or []),
    }
