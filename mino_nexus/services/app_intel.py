"""AppIntel 信息基座：三渠道（Nav / Knowledge / Doc）统一检索与 context-pack 编排。

设计稿 docs/9月12日_AppIntel信息基座.md
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from mino_nexus.services import nav_compiler
from mino_nexus.services import nav_fsm as F
from mino_nexus.services import nav_fsm_store as nav_store
from mino_nexus.services import nav_route

DEFAULT_BUDGETS: dict[str, int] = {
    "nav_assist": 1200,
    "knowledge": 1200,
    "doc_context": 800,
}

_KIND_SET = frozenset({"nav", "knowledge", "doc"})


@dataclass
class ContextPack:
    knowledge_hint: str = ""
    knowledge_body: str = ""
    doc_context: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    policy_applied: str = ""
    know_rows: list[dict[str, Any]] = field(default_factory=list)
    wiki_guard_text: str = ""

    def to_slots(self) -> dict[str, str]:
        return {
            "knowledge_hint": self.knowledge_hint,
            "knowledge_body": self.knowledge_body,
            "doc_context": self.doc_context,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "knowledge_hint": self.knowledge_hint,
            "knowledge_body": self.knowledge_body,
            "doc_context": self.doc_context,
            "citations": list(self.citations),
            "skipped": list(self.skipped),
            "policy_applied": self.policy_applied,
        }


def _now() -> int:
    return int(time.time())


def _parse_kinds(raw: str) -> set[str]:
    bits = {str(x).strip().lower() for x in str(raw or "").split(",") if str(x).strip()}
    if not bits:
        return set(_KIND_SET)
    return {k for k in bits if k in _KIND_SET} or set(_KIND_SET)


def _resolve_project_id(app_id: str, project_id: str = "") -> str:
    pid = str(project_id or "").strip()
    if pid:
        return pid
    try:
        from mino_nexus.services import project_store as ps

        app = ps.find_app(app_id)
        if app:
            return str(app.get("project_id") or "").strip()
    except Exception:
        pass
    return ""


def wiki_refs_for_state(fsm: dict[str, Any], state_id: str) -> list[str]:
    """收集 state 级与 guards 内嵌的 wiki_ref。"""
    sid = str(state_id or "").strip()
    if not sid or not fsm:
        return []
    st = F.state_by_id(fsm, sid)
    if not st:
        return []
    refs: list[str] = []
    top = str(st.get("wiki_ref") or "").strip()
    if top:
        refs.append(top)

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            wr = str(obj.get("wiki_ref") or "").strip()
            if wr:
                refs.append(wr)
            for val in obj.values():
                walk(val)
        elif isinstance(obj, list):
            for val in obj:
                walk(val)

    walk(st.get("guards") or {})
    out: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def _knowledge_sufficient(know_rows: list[dict[str, Any]], wiki_text: str) -> bool:
    if str(wiki_text or "").strip():
        return True
    from mino_nexus.services.knowledge_situation import SIT_MATCH_USED

    for row in know_rows or []:
        if row.get("used") is False:
            continue
        via = str(row.get("used_via") or "").strip()
        if via in ("bind", "situation"):
            return True
        if int(row.get("sit_score") or 0) >= SIT_MATCH_USED:
            return True
        if int(row.get("score") or 0) >= 2:
            return True
    return False


def _citations_from_knowledge(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if row.get("used") is False:
            continue
        kid = str(row.get("id") or "").strip()
        if not kid:
            continue
        out.append({
            "kind": "knowledge",
            "id": kid,
            "title": str(row.get("title") or "").strip(),
            "ref": f"knowledge:{kid}",
        })
    return out


def _citations_from_docs(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in hits or []:
        cid = str(row.get("chunk_id") or "").strip()
        if not cid:
            continue
        out.append({
            "kind": "doc",
            "id": cid,
            "title": str(row.get("title") or "").strip(),
            "heading": str(row.get("heading") or "").strip(),
            "ref": f"doc:chunk:{cid}",
        })
    return out


def context_pack_for_step(
    *,
    ctx,
    case: dict[str, Any],
    cursor,
    history: list[str],
    steps: list[dict[str, Any]],
    hierarchy_text: str = "",
    state_id: str = "",
    policy: str = "wiki_first",
    scripted_check: bool = False,
    doc_limit: int = 3,
) -> ContextPack:
    """跑批每步编排：知识 + 文档，wiki_first 下知识充足则跳过 doc。"""
    from mino_nexus.ai.knowledge_hint import (
        build_index_text,
        pick_auto_knowledge_body,
        should_auto_inject_do_body,
    )
    from mino_nexus.loop.inspections import match_step_docs, match_step_knowledge, resolve_knowledge_scope

    pack = ContextPack(policy_applied=str(policy or "wiki_first").strip() or "wiki_first")
    app_id, project_id = resolve_knowledge_scope(ctx)

    know_rows = match_step_knowledge(
        ctx=ctx,
        case=case,
        cursor=cursor,
        history=history,
        steps=steps,
        hierarchy_text=hierarchy_text,
    )
    pack.know_rows = know_rows
    pack.knowledge_hint = build_index_text(know_rows)
    pack.citations.extend(_citations_from_knowledge(know_rows))

    wiki_text = ""
    sid = str(state_id or "").strip()
    if sid and app_id:
        fsm = nav_store.load(app_id) or {}
        refs = wiki_refs_for_state(fsm, sid)
        if refs:
            wiki_text = nav_compiler.wiki_block(refs, app_id=app_id, project_id=project_id)
            pack.wiki_guard_text = wiki_text
            pack.citations.append({
                "kind": "nav",
                "ref": f"nav:{sid}",
                "state_id": sid,
                "wiki_refs": refs,
            })

    auto_body = ""
    if scripted_check:
        auto_body = pick_auto_knowledge_body(know_rows)
    elif should_auto_inject_do_body(know_rows):
        auto_body = pick_auto_knowledge_body(know_rows)
    pack.knowledge_body = auto_body

    skip_doc = False
    if pack.policy_applied == "wiki_first" and _knowledge_sufficient(know_rows, wiki_text):
        skip_doc = True
        pack.skipped.append("doc_context")

    if not skip_doc and app_id:
        pack.doc_context = match_step_docs(
            ctx=ctx,
            case=case,
            cursor=cursor,
            history=history,
            steps=steps,
            hierarchy_text=hierarchy_text,
            limit=doc_limit,
        )
        if pack.doc_context:
            try:
                from mino_nexus.services import doc_store as ds

                from mino_nexus.ai.knowledge_hint import build_case_intent, build_query, build_step_focus

                intent = build_case_intent(
                    case_name=str(case.get("name") or ""),
                    goal=cursor.decide_goal(),
                    steps_text=cursor.prompt_block(),
                    precondition=str(case.get("precondition") or ""),
                    success_criteria=cursor.decide_success(),
                )
                last = ""
                if steps:
                    tail = steps[-1] or {}
                    last = " ".join(str(x) for x in (
                        tail.get("capability_id") or "",
                        tail.get("thought") or "",
                        tail.get("summary") or "",
                    ) if x)
                query = build_query(
                    case_intent=intent,
                    step_focus=build_step_focus(cursor),
                    last_action=last,
                    history="\n".join(history[-6:]) if history else "",
                    screen=str(hierarchy_text or "")[:1600],
                )
                hits = ds.search_documents(q=query, app_id=app_id, limit=doc_limit)
                pack.citations.extend(_citations_from_docs(hits))
            except Exception:
                pass

    cap = DEFAULT_BUDGETS["doc_context"]
    if len(pack.doc_context) > cap:
        pack.doc_context = pack.doc_context[:cap] + "…"

    return pack


def context_pack_http(
    *,
    app_id: str,
    project_id: str = "",
    intent: str = "",
    state_id: str = "",
    hierarchy_excerpt: str = "",
    policy: str = "wiki_first",
    budgets: dict[str, int] | None = None,
) -> dict[str, Any]:
    """HTTP /context-pack：用 intent 字符串检索，不依赖 RunContext。"""
    from mino_nexus.services import doc_match as dm
    from mino_nexus.services import doc_store as ds
    from mino_nexus.services.knowledge_match import match_knowledge

    aid = str(app_id or "").strip()
    pid = _resolve_project_id(aid, project_id)
    query = str(intent or hierarchy_excerpt or "").strip()
    pol = str(policy or "wiki_first").strip() or "wiki_first"
    bud = dict(DEFAULT_BUDGETS)
    if isinstance(budgets, dict):
        bud.update({k: int(v) for k, v in budgets.items() if k in bud})

    pack = ContextPack(policy_applied=pol)
    sid = str(state_id or "").strip()

    wiki_text = ""
    if sid:
        fsm = nav_store.load(aid) or {}
        refs = wiki_refs_for_state(fsm, sid)
        if refs:
            wiki_text = nav_compiler.wiki_block(refs, app_id=aid, project_id=pid)
            pack.wiki_guard_text = wiki_text
            pack.citations.append({
                "kind": "nav",
                "ref": f"nav:{sid}",
                "state_id": sid,
                "wiki_refs": refs,
            })

    know_rows: list[dict[str, Any]] = []
    if query and (aid or pid):
        try:
            know_rows = match_knowledge(query, app_id=aid, project_id=pid, limit=3)
        except Exception:
            know_rows = []
    from mino_nexus.ai.knowledge_hint import build_index_text

    pack.know_rows = know_rows
    pack.knowledge_hint = build_index_text(know_rows)[: bud["knowledge"]]
    pack.citations.extend(_citations_from_knowledge(know_rows))

    skip_doc = pol == "wiki_first" and _knowledge_sufficient(know_rows, wiki_text)
    if skip_doc:
        pack.skipped.append("doc_context")
    elif query and aid:
        try:
            _, block = dm.match_step_docs(query, app_id=aid, limit=3)
            pack.doc_context = block[: bud["doc_context"]]
            hits = ds.search_documents(q=query, app_id=aid, limit=3)
            pack.citations.extend(_citations_from_docs(hits))
        except Exception:
            pass

    out = pack.to_dict()
    out["nav_assist"] = ""
    out["wiki_guard_text"] = wiki_text[: bud["nav_assist"]]
    return out


def search(
    *,
    app_id: str,
    q: str,
    kinds: str = "nav,knowledge,doc",
    state_id: str = "",
    project_id: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """统一检索：返回 AppIntelEntry 逻辑视图列表。"""
    aid = str(app_id or "").strip()
    query = str(q or "").strip()
    if not aid or not query:
        return []
    pid = _resolve_project_id(aid, project_id)
    want = _parse_kinds(kinds)
    lim = max(1, min(50, int(limit or 20)))
    per_kind = max(3, lim // max(1, len(want)))
    out: list[dict[str, Any]] = []

    if "knowledge" in want:
        try:
            from mino_nexus.services.knowledge_match import match_knowledge

            for row in match_knowledge(query, app_id=aid, project_id=pid, limit=per_kind):
                kid = str(row.get("id") or "").strip()
                if not kid:
                    continue
                out.append({
                    "kind": "knowledge",
                    "ref": f"knowledge:{kid}",
                    "title": str(row.get("title") or "").strip(),
                    "summary": str(row.get("content") or "")[:240],
                    "score": float(row.get("score") or 0),
                    "tags": list(row.get("tags") or []),
                })
        except Exception:
            pass

    if "doc" in want:
        try:
            from mino_nexus.services import doc_store as ds

            for row in ds.search_documents(q=query, app_id=aid, limit=per_kind):
                cid = str(row.get("chunk_id") or "").strip()
                if not cid:
                    continue
                out.append({
                    "kind": "doc_chunk",
                    "ref": f"doc:chunk:{cid}",
                    "title": str(row.get("title") or "").strip(),
                    "summary": str(row.get("snippet") or row.get("text") or "")[:240],
                    "score": float(row.get("hybrid_score") or 0),
                    "source_id": str(row.get("source_id") or ""),
                })
        except Exception:
            pass

    if "nav" in want:
        fsm = nav_store.read_raw(aid) or {}
        qlow = query.lower()
        sid_filter = str(state_id or "").strip()
        for st in fsm.get("states") or []:
            if not isinstance(st, dict):
                continue
            sid = str(st.get("id") or "").strip()
            if not sid:
                continue
            if sid_filter and sid != sid_filter and sid_filter not in sid:
                continue
            role = str(st.get("role") or "").strip()
            blob = f"{sid} {role}".lower()
            if qlow not in blob and qlow not in str(st.get("identify") or "").lower():
                continue
            refs = wiki_refs_for_state(fsm, sid)
            out.append({
                "kind": "nav_state",
                "ref": f"nav:{sid}",
                "title": sid,
                "summary": role or str(st.get("kind") or "page"),
                "wiki_refs": refs,
            })
        for ed in fsm.get("edges") or []:
            if not isinstance(ed, dict):
                continue
            eid = str(ed.get("id") or "").strip()
            blob = f"{eid} {ed.get('from')} {ed.get('to')}".lower()
            if qlow not in blob:
                continue
            out.append({
                "kind": "nav_edge",
                "ref": f"nav:edge:{eid}",
                "title": eid,
                "summary": f"{ed.get('from')} → {ed.get('to')}",
            })

    out.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    return out[:lim]


def route_plan(
    *,
    app_id: str,
    from_state: str,
    to_state: str,
    project_id: str = "",
    use_live: bool = True,
) -> dict[str, Any]:
    """路径规划 + 沿途 wiki_ref 摘要。"""
    aid = str(app_id or "").strip()
    fsm, source = nav_route.load_fsm_doc(aid, project_id=project_id, use_live=use_live)
    if not fsm:
        return {"ok": False, "error": "无 NavFSM 配置", "source": source}
    plan = nav_route.plan_route(fsm, from_state=from_state, to_state=to_state)
    pid = _resolve_project_id(aid, project_id)
    wiki_bits: list[dict[str, Any]] = []
    seen_states: set[str] = set()
    for step in plan.get("steps") or []:
        for key in ("from", "to"):
            sid = str(step.get(key) or "").strip()
            if not sid or sid in seen_states:
                continue
            seen_states.add(sid)
            refs = wiki_refs_for_state(fsm, sid)
            if refs:
                wiki_bits.append({
                    "state_id": sid,
                    "wiki_refs": refs,
                    "excerpt": nav_compiler.wiki_block(refs, app_id=aid, project_id=pid),
                })
    plan["wiki_along_route"] = wiki_bits
    plan["source"] = source
    return plan


def graph(
    *,
    app_id: str,
    center: str,
    depth: int = 1,
    project_id: str = "",
) -> dict[str, Any]:
    """以 nav 节点为中心的子图：邻接边 + 挂接知识 + 额外 links。"""
    aid = str(app_id or "").strip()
    fsm = nav_store.read_raw(aid) or {}
    if not fsm:
        return {"ok": False, "error": "无 NavFSM 配置", "nodes": [], "edges": []}

    raw = str(center or "").strip()
    if raw.startswith("nav:"):
        raw = raw[4:]
    if raw.startswith("edge:"):
        raw = raw[5:]
    sid = nav_route.resolve_state_ref(fsm, raw) or raw
    dep = max(0, min(3, int(depth or 1)))
    pid = _resolve_project_id(aid, project_id)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    visited: set[str] = {sid}

    def add_state(state_id: str) -> None:
        st = F.state_by_id(fsm, state_id)
        if not st:
            return
        refs = wiki_refs_for_state(fsm, state_id)
        knowledge: list[dict[str, Any]] = []
        if refs:
            try:
                from mino_nexus.services.knowledge_match import items_by_ids

                knowledge = items_by_ids(
                    nav_compiler.entry_ids(refs),
                    app_id=aid,
                    project_id=pid,
                )
            except Exception:
                knowledge = []
        nodes.append({
            "kind": "nav_state",
            "ref": f"nav:{state_id}",
            "state_id": state_id,
            "role": str(st.get("role") or ""),
            "wiki_refs": refs,
            "knowledge": [{"id": k.get("id"), "title": k.get("title")} for k in knowledge],
        })

    def expand(state_id: str, d: int) -> None:
        add_state(state_id)
        if d <= 0:
            return
        for ed in F.edges_from(fsm, state_id):
            eid = str(ed.get("id") or "")
            tgt = str(ed.get("to") or "")
            edges.append({
                "kind": "nav_edge",
                "ref": f"nav:edge:{eid}",
                "edge_id": eid,
                "from": str(ed.get("from") or ""),
                "to": tgt,
            })
            if tgt and tgt not in visited:
                visited.add(tgt)
                expand(tgt, d - 1)
        for ed in fsm.get("edges") or []:
            if str(ed.get("to") or "") != state_id:
                continue
            src = str(ed.get("from") or "")
            eid = str(ed.get("id") or "")
            edges.append({
                "kind": "nav_edge",
                "ref": f"nav:edge:{eid}",
                "edge_id": eid,
                "from": src,
                "to": state_id,
            })
            if src and src not in visited and dep > 0:
                visited.add(src)
                expand(src, d - 1)

    if sid:
        expand(sid, dep)

    extra_links = list_links(app_id=aid, from_ref=f"nav:{sid}") if sid else []
    doc_refs = [lk for lk in extra_links if str(lk.get("to_ref") or "").startswith("doc:")]

    return {
        "ok": True,
        "center": f"nav:{sid}",
        "depth": dep,
        "nodes": nodes,
        "edges": edges,
        "extra_links": extra_links,
        "doc_links": doc_refs,
    }


def gaps(
    *,
    app_id: str,
    project_id: str = "",
) -> dict[str, Any]:
    """缺口扫描：无 wiki_ref 的 state、文档/知识空库等。"""
    aid = str(app_id or "").strip()
    pid = _resolve_project_id(aid, project_id)
    fsm = nav_store.read_raw(aid) or {}

    missing_wiki: list[dict[str, Any]] = []
    for st in fsm.get("states") or []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or "").strip()
        if not sid:
            continue
        refs = wiki_refs_for_state(fsm, sid)
        if not refs:
            missing_wiki.append({
                "state_id": sid,
                "role": str(st.get("role") or ""),
                "kind": str(st.get("kind") or "page"),
            })

    doc_count = 0
    know_count = 0
    pending_knowledge = 0
    try:
        from mino_nexus.services import doc_store as ds

        doc_count = len(ds.list_sources(app_id=aid, project_id=pid))
    except Exception:
        pass
    try:
        from mino_nexus.core.database import SessionLocal, ensure_db
        from mino_nexus.models.knowledge import KnowledgeEntry

        ensure_db()
        db = SessionLocal()
        try:
            rows = db.query(KnowledgeEntry).filter(KnowledgeEntry.enabled.is_(True)).all()
            for row in rows:
                aids = list(row.app_ids_json or [])
                if aids and aid not in aids and pid not in aids:
                    continue
                know_count += 1
                if str(row.review_status or "") == "pending":
                    pending_knowledge += 1
        finally:
            db.close()
    except Exception:
        pass

    proposals_pending = len(list_proposals(app_id=aid, status="pending"))

    return {
        "app_id": aid,
        "project_id": pid,
        "has_nav_fsm": bool(fsm.get("states")),
        "states_missing_wiki": missing_wiki,
        "states_missing_wiki_count": len(missing_wiki),
        "doc_source_count": doc_count,
        "knowledge_count": know_count,
        "knowledge_pending": pending_knowledge,
        "proposals_pending": proposals_pending,
        "empty_doc_library": doc_count == 0,
    }


def ask(
    *,
    app_id: str,
    question: str,
    project_id: str = "",
    kinds: str = "nav,knowledge,doc",
    limit: int = 8,
) -> dict[str, Any]:
    """检索 + LLM 问答，返回 answer 与 citations。"""
    from mino_nexus.ai.llm_client import call_chat_plain, resolve_regression_provider

    aid = str(app_id or "").strip()
    q = str(question or "").strip()
    if not aid or not q:
        return {"ok": False, "error": "app_id / question 不能为空"}

    hits = search(app_id=aid, q=q, kinds=kinds, project_id=project_id, limit=limit)
    citations = [
        {
            "kind": h.get("kind"),
            "ref": h.get("ref"),
            "title": h.get("title"),
        }
        for h in hits
    ]
    ctx_lines: list[str] = []
    for i, h in enumerate(hits[:limit], 1):
        ctx_lines.append(
            f"[{i}] ({h.get('kind')}) {h.get('title')}: {str(h.get('summary') or '')[:500]}"
        )
    context = "\n".join(ctx_lines) or "（检索无命中）"

    provider, gate = resolve_regression_provider()
    if not provider:
        return {
            "ok": False,
            "error": gate.get("reason") or "未配置可用的大模型",
            "citations": citations,
            "context_preview": context[:2000],
        }

    system = (
        "你是被测应用的信息助手。仅根据【检索上下文】作答；"
        "无法从上下文得出结论时明确说不知道。"
        "回答末尾用「引用：」列出用到的 [编号]。"
    )
    user = f"问题：{q}\n\n【检索上下文】\n{context}"
    text, meta = call_chat_plain(
        provider=provider,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user[:12000]},
        ],
        temperature=0.2,
        max_tokens=2048,
        timeout_sec=90,
    )
    if not text:
        return {
            "ok": False,
            "error": str(meta.get("error") or "模型无返回"),
            "citations": citations,
            "meta": meta,
        }
    return {
        "ok": True,
        "answer": text,
        "citations": citations,
        "meta": {"dispatch_id": meta.get("dispatch_id")},
    }


# ---------------- links ----------------


def list_links(
    *,
    app_id: str,
    from_ref: str = "",
    to_ref: str = "",
    rel: str = "",
) -> list[dict[str, Any]]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.app_intel import AppIntelLink

    aid = str(app_id or "").strip()
    if not aid:
        return []
    ensure_db()
    db = SessionLocal()
    try:
        q = db.query(AppIntelLink).filter(AppIntelLink.app_id == aid)
        if from_ref:
            q = q.filter(AppIntelLink.from_ref == str(from_ref).strip())
        if to_ref:
            q = q.filter(AppIntelLink.to_ref == str(to_ref).strip())
        if rel:
            q = q.filter(AppIntelLink.rel == str(rel).strip())
        rows = q.order_by(AppIntelLink.id.desc()).limit(200).all()
        return [
            {
                "id": row.id,
                "app_id": row.app_id,
                "from_ref": row.from_ref,
                "to_ref": row.to_ref,
                "rel": row.rel,
                "meta": dict(row.meta_json or {}),
                "created_at": int(row.created_at or 0),
            }
            for row in rows
        ]
    finally:
        db.close()


def add_link(
    *,
    app_id: str,
    from_ref: str,
    to_ref: str,
    rel: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.app_intel import AppIntelLink

    aid = str(app_id or "").strip()
    fr = str(from_ref or "").strip()
    tr = str(to_ref or "").strip()
    rl = str(rel or "").strip()
    if not aid or not fr or not tr or not rl:
        return {"ok": False, "error": "app_id / from_ref / to_ref / rel 必填"}
    ensure_db()
    db = SessionLocal()
    try:
        row = AppIntelLink(
            app_id=aid,
            from_ref=fr,
            to_ref=tr,
            rel=rl,
            meta_json=dict(meta or {}),
            created_at=_now(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "ok": True,
            "id": row.id,
            "app_id": row.app_id,
            "from_ref": row.from_ref,
            "to_ref": row.to_ref,
            "rel": row.rel,
        }
    finally:
        db.close()


# ---------------- proposals ----------------


def list_proposals(
    *,
    app_id: str,
    status: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.app_intel import AppIntelProposal

    aid = str(app_id or "").strip()
    if not aid:
        return []
    ensure_db()
    db = SessionLocal()
    try:
        q = db.query(AppIntelProposal).filter(AppIntelProposal.app_id == aid)
        st = str(status or "").strip()
        if st:
            q = q.filter(AppIntelProposal.status == st)
        rows = q.order_by(AppIntelProposal.created_at.desc()).limit(max(1, min(200, limit))).all()
        return [_proposal_public(r) for r in rows]
    finally:
        db.close()


def _proposal_public(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "app_id": row.app_id,
        "kind": row.kind,
        "status": row.status,
        "payload": dict(row.payload_json or {}),
        "note": row.note or "",
        "created_at": int(row.created_at or 0),
        "updated_at": int(row.updated_at or 0),
    }


def review_proposal(
    *,
    app_id: str,
    proposal_id: str,
    status: str,
    note: str = "",
) -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.app_intel import AppIntelProposal

    aid = str(app_id or "").strip()
    pid = str(proposal_id or "").strip()
    st = str(status or "").strip().lower()
    if not aid or not pid:
        return {"ok": False, "error": "app_id / proposal_id 必填"}
    if st not in {"approved", "rejected", "pending"}:
        return {"ok": False, "error": "status 须为 approved / rejected / pending"}
    ensure_db()
    db = SessionLocal()
    try:
        row = (
            db.query(AppIntelProposal)
            .filter(AppIntelProposal.app_id == aid, AppIntelProposal.id == pid)
            .one_or_none()
        )
        if row is None:
            return {"ok": False, "error": "proposal 不存在"}
        row.status = st
        if note:
            row.note = str(note).strip()
        row.updated_at = _now()
        db.commit()
        return {"ok": True, "item": _proposal_public(row)}
    finally:
        db.close()


def create_proposal(
    *,
    app_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    note: str = "",
) -> dict[str, Any]:
    from mino_nexus.core.database import SessionLocal, ensure_db
    from mino_nexus.models.app_intel import AppIntelProposal

    aid = str(app_id or "").strip()
    kd = str(kind or "").strip()
    if not aid or not kd:
        return {"ok": False, "error": "app_id / kind 必填"}
    ensure_db()
    pid = f"prop-{uuid.uuid4().hex[:10]}"
    now = _now()
    db = SessionLocal()
    try:
        row = AppIntelProposal(
            id=pid,
            app_id=aid,
            kind=kd,
            status="pending",
            payload_json=dict(payload or {}),
            note=str(note or "").strip(),
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.commit()
        return {"ok": True, "item": _proposal_public(row)}
    finally:
        db.close()


def log_context_pack(writer, pack: ContextPack) -> None:
    """写入 session log：intel/context_pack。"""
    if writer is None:
        return
    try:
        writer.append(
            "intel/context_pack",
            {
                "policy": pack.policy_applied,
                "skipped": list(pack.skipped),
                "citations": list(pack.citations)[:20],
                "has_knowledge_hint": bool(pack.knowledge_hint),
                "has_knowledge_body": bool(pack.knowledge_body),
                "has_doc_context": bool(pack.doc_context),
                "has_wiki_guard": bool(pack.wiki_guard_text),
            },
        )
    except Exception:
        pass


__all__ = [
    "ContextPack",
    "DEFAULT_BUDGETS",
    "add_link",
    "ask",
    "context_pack_for_step",
    "context_pack_http",
    "create_proposal",
    "gaps",
    "graph",
    "list_links",
    "list_proposals",
    "review_proposal",
    "log_context_pack",
    "route_plan",
    "search",
    "wiki_refs_for_state",
]
