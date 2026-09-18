"""一次性：把 published `v1` 从 Atlas 重发一遍，让 `page.sk*` 带着 display_name 落库。

背景见 docs/9月16日-用例执行调用fsmnavigate能力异常.md §0.8：`nav_fsm_states` 以前没有
meta 列，发布时 display_name / aliases 被丢掉；同时 `v1` 里还留着上一代 `page.tab_*`
占位节点，与骨骼节点互不连通，`fsm_navigate` 因此只会 BACK。

    python scripts/republish_nav_fsm_from_atlas.py <app_id> [--write]

不带 `--write` 只做对照打印，不落库。
"""
from __future__ import annotations

import argparse
import json
from typing import Any


def _brief(doc: dict[str, Any] | None) -> dict[str, Any]:
    doc = doc or {}
    states = doc.get("states") or []
    return {
        "states": len(states),
        "edges": len(doc.get("edges") or []),
        "legacy_tab": [
            str(s.get("id"))
            for s in states
            if str((s or {}).get("id") or "").startswith("page.tab_")
        ],
        "named": sum(
            1
            for s in states
            if str(((s or {}).get("meta") or {}).get("display_name") or "").strip()
        ),
    }


def _dump_states(doc: dict[str, Any], title: str) -> None:
    print(f"\n--- {title}")
    for st in doc.get("states") or []:
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        aliases = [str(a) for a in (meta.get("aliases") or [])][:6]
        print(
            f"  {st.get('id'):32} entry={bool(st.get('entry')):<5} "
            f"display={meta.get('display_name') or '-':<12} aliases={aliases}"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("app_id")
    ap.add_argument("--write", action="store_true", help="真正写入 v1")
    args = ap.parse_args()

    from mino_nexus.services import nav_fsm_store as store
    from mino_nexus.services import nav_route, project_store as ps
    from mino_nexus.services.nav_candidate_compiler import finalize_for_publish
    from mino_nexus.services.nav_edge_resolve import enrich_state_aliases_from_nav_edges
    from mino_nexus.services.nav_screen_registry import atlas_doc_for_navigation
    from mino_nexus.services.nav_state_resolve import resolve_state_fuzzy

    app_id = args.app_id
    project_id = str((ps.find_app(app_id) or {}).get("project_id") or "")

    before = store.read_raw(app_id)
    print("v1 现状:", json.dumps(_brief(before), ensure_ascii=False))
    runtime_before, src = nav_route.load_fsm_doc(app_id, use_live=False)
    print(f"跑批实际看到（source={src}）:", json.dumps(_brief(runtime_before), ensure_ascii=False))
    if before:
        _dump_states(before, "重发前 v1")

    doc = atlas_doc_for_navigation(app_id, project_id=project_id)
    if not doc:
        print("Atlas 为空（采集不足），不重发。")
        return 1
    doc = dict(doc)
    doc["app_id"] = app_id
    doc["project_id"] = project_id or str(doc.get("project_id") or "")
    doc = finalize_for_publish(app_id, doc)
    doc, alias_hits = enrich_state_aliases_from_nav_edges(doc)
    print(f"\nAtlas 重建: {json.dumps(_brief(doc), ensure_ascii=False)} 别名回填 {alias_hits} 条")
    _dump_states(doc, "重发后（待写入）")

    if not args.write:
        print("\n（dry-run，未写库。加 --write 落库。）")
        return 0

    saved = store.promote_prepared(app_id, doc, updated_by="republish_from_atlas")
    print("\n已写入 v1:", json.dumps(_brief(saved), ensure_ascii=False))

    runtime, src = nav_route.load_fsm_doc(app_id, use_live=False)
    print(f"跑批复核（source={src}）:", json.dumps(_brief(runtime), ensure_ascii=False))
    for st in runtime.get("states") or []:
        meta = st.get("meta") if isinstance(st.get("meta"), dict) else {}
        name = str(meta.get("display_name") or "").strip()
        if not name:
            continue
        out = resolve_state_fuzzy(runtime, name, role="to")
        print(f"  解析「{name}」→ {out.state_id} (name={out.name_score:.2f} {out.method})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
