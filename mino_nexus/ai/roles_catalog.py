"""角色目录：产品身份 + llm_jobs system prompt。"""
from __future__ import annotations

from typing import Any, Optional

_EDITABLE_ROLE_IDS = {"im-qa-assistant", "im-defect-assistant"}
_MAX_HISTORY = 20
_MAX_CONTENT = 8000

ROLE_JOB_MAP: dict[str, str] = {
    "conductor": "conductor",
    "req-analyst": "analyze_req",
    "mindmap-writer": "draft_mindmap",
    "case-writer": "draft_cases",
    "req-qa-bm": "req-qa-bm",
    "version-qa-bm": "version-qa-bm",
    "test-engineer": "test-engineer-chat",
    "report-writer": "report-writer",
    "doc-keeper": "doc-keeper",
    "im-qa-assistant": "im-dialogue",
    "im-defect-assistant": "im-defect",
    "knowledge-reviewer": "knowledge-reviewer",
    "product-expert": "product-expert",
    "propose_atlas": "propose_atlas",
    "agent-decide": "agent-decide",
}


def _role(
    *,
    id: str,
    label: str,
    group: str,
    kind: str,
    source: str,
    used_in: list[str],
    summary: str,
    system_prompt: str,
    related_ids: Optional[list[str]] = None,
    live: bool = True,
    owner: str = "",
    job: str = "",
    called: str = "",
    triggers: Optional[list[str]] = None,
) -> dict[str, Any]:
    prompt = str(system_prompt or "").strip()
    return {
        "id": id,
        "label": label,
        "group": group,
        "kind": kind,
        "source": source,
        "used_in": used_in,
        "summary": summary,
        "system_prompt": prompt,
        "prompt_chars": len(prompt),
        "related_ids": related_ids or [],
        "live": live,
        "owner": owner,
        "job": job,
        "called": called or ("wired" if live else "sandbox"),
        "triggers": triggers or [],
        "output": "json" if kind == "json" else "text",
        "editable": id in _EDITABLE_ROLE_IDS,
        "default_prompt": prompt,
        "prompt_custom": False,
    }


def _job_prompt(role_or_job_id: str) -> str:
    from mino_nexus.services.job_store import job_system_text, resolve_job_id

    jid = ROLE_JOB_MAP.get(role_or_job_id) or resolve_job_id(role_or_job_id)
    return job_system_text(jid)


def _apply_role_prompt(row: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.services.settings_store import get_role_prompt_override

    out = dict(row)
    rid = str(out.get("id") or "")
    built_in = _job_prompt(rid) or str(out.get("default_prompt") or out.get("system_prompt") or "").strip()
    out["default_prompt"] = built_in
    override = get_role_prompt_override(rid)
    if override:
        out["system_prompt"] = override
        out["prompt_chars"] = len(override)
        out["prompt_custom"] = True
    else:
        out["system_prompt"] = built_in
        out["prompt_chars"] = len(built_in)
        out["prompt_custom"] = False
    return out


def _product_roles() -> list[dict[str, Any]]:
    return [
        _role(
            id="conductor", label="分析师", group="abstract", kind="json",
            source="llm_jobs",
            used_in=["route", "qa_tick", "case_exec_agent"],
            summary="抽象调度：看当前步骤该调用哪个角色的哪项能力。",
            system_prompt=_job_prompt("conductor"),
            related_ids=["req-analyst", "test-engineer", "req-qa-bm"],
            live=True, owner="conductor", job="route", called="wired",
            triggers=["流程 tick 选下一步", "设置页问「这一步该调谁」"],
        ),
        _role(
            id="req-analyst", label="需求分析师", group="product", kind="json",
            source="llm_jobs",
            used_in=["analyze_req", "propose_atlas"],
            summary="读需求原文，拆验收标准和测试点。",
            system_prompt=_job_prompt("req-analyst"),
            related_ids=["mindmap-writer", "case-writer", "req-qa-bm"],
            live=True, owner="req-analyst", job="analyze_req", called="wired",
            triggers=["新建需求", "流程 tick"],
        ),
        _role(
            id="mindmap-writer", label="测试脑图编写", group="product", kind="json",
            source="llm_jobs",
            used_in=["draft_mindmap"],
            summary="沿入口和端铺覆盖脑图。",
            system_prompt=_job_prompt("mindmap-writer"),
            related_ids=["req-analyst", "case-writer"],
            live=True, owner="mindmap-writer", job="draft_mindmap", called="wired",
            triggers=["需求分析完成", "流程 tick"],
        ),
        _role(
            id="case-writer", label="测试用例编写", group="product", kind="json",
            source="llm_jobs",
            used_in=["draft_cases"],
            summary="按测试点展开用例草稿。",
            system_prompt=_job_prompt("case-writer"),
            related_ids=["req-analyst", "mindmap-writer"],
            live=True, owner="case-writer", job="draft_cases", called="wired",
            triggers=["测试点缺用例", "流程 tick"],
        ),
        _role(
            id="req-qa-bm", label="需求QA BM", group="product", kind="conversational",
            source="llm_jobs",
            used_in=["map_cases", "classify_fail", "draft_sign"],
            summary="一条需求从覆盖到验收。结论必须人审。",
            system_prompt=_job_prompt("req-qa-bm"),
            related_ids=["req-analyst", "case-writer"],
            live=True, owner="req-qa-bm", job="draft_sign", called="wired",
        ),
        _role(
            id="version-qa-bm", label="版本QA BM", group="product", kind="conversational",
            source="llm_jobs",
            used_in=["pick_regression", "draft_gate"],
            summary="这版带哪些需求出门、历史回归与发版评审。",
            system_prompt=_job_prompt("version-qa-bm"),
            related_ids=["req-qa-bm"],
            live=True, owner="version-qa-bm", job="draft_gate", called="wired",
        ),
        _role(
            id="test-engineer", label="测试工程师", group="product", kind="conversational",
            source="llm_jobs",
            used_in=["用例执行", "Agent"],
            summary="在设备上执行用例并给出屏幕证据。",
            system_prompt=_job_prompt("test-engineer"),
            related_ids=["agent-decide"],
            live=True, owner="test-engineer", called="wired",
            triggers=["下发任务", "Case Runner"],
        ),
        _role(
            id="report-writer", label="报告编写", group="product", kind="json",
            source="llm_jobs",
            used_in=["测试报告"],
            summary="按流程事实写报告草稿。",
            system_prompt=_job_prompt("report-writer"),
            related_ids=["req-qa-bm", "doc-keeper"],
            live=True, owner="report-writer", job="draft_test_report", called="sandbox",
        ),
        _role(
            id="doc-keeper", label="文档维护", group="product", kind="json",
            source="llm_jobs",
            used_in=["文档插件"],
            summary="把人确认过的内容同步到外部文档。",
            system_prompt=_job_prompt("doc-keeper"),
            related_ids=["report-writer"],
            live=True, owner="doc-keeper", job="publish_wiki", called="sandbox",
        ),
        _role(
            id="im-qa-assistant", label="IM 总指挥", group="product", kind="conversational",
            source="llm_jobs",
            used_in=["角色 · prompt"],
            summary="IM 通道里的总指挥。prompt 可在设置页改。",
            system_prompt=_job_prompt("im-qa-assistant"),
            related_ids=["im-defect-assistant"],
            live=True, owner="im-qa-assistant", called="wired",
        ),
        _role(
            id="im-defect-assistant", label="IM 缺陷助手", group="product", kind="json",
            source="llm_jobs",
            used_in=["IM 提缺陷"],
            summary="把缺陷整理成 JSON。prompt 可在设置页改。",
            system_prompt=_job_prompt("im-defect-assistant"),
            related_ids=["im-qa-assistant"],
            live=True, owner="im-defect-assistant", called="wired",
        ),
        _role(
            id="knowledge-reviewer", label="知识审核员", group="product", kind="json",
            source="llm_jobs",
            used_in=["知识库机审"],
            summary="审执行沉淀的知识草稿。",
            system_prompt=_job_prompt("knowledge-reviewer"),
            related_ids=["product-expert"],
            live=True, owner="knowledge-reviewer", job="review_knowledge", called="wired",
        ),
        _role(
            id="product-expert", label="产品专家", group="product", kind="json",
            source="llm_jobs",
            used_in=["应用简报"],
            summary="了解被测应用的图谱、功能和需求。",
            system_prompt=_job_prompt("product-expert"),
            related_ids=["knowledge-reviewer", "req-analyst"],
            live=True, owner="product-expert", job="brief_knowledge", called="wired",
        ),
    ]


def _runtime_roles() -> list[dict[str, Any]]:
    return [
        _role(
            id="propose_atlas", label="影响范围与骨架变更", group="runtime", kind="json",
            source="llm_jobs",
            used_in=["propose_atlas"],
            summary="判断需求影响哪些模块/功能。只出品变更，等人确认。",
            system_prompt=_job_prompt("propose_atlas"),
            owner="req-analyst", job="propose_atlas",
        ),
        _role(
            id="agent-decide", label="看图决策", group="runtime", kind="json",
            source="llm_jobs",
            used_in=["Agent 逐步执行"],
            summary="每一步看截图决定下一个动作。",
            system_prompt=_job_prompt("agent-decide"),
            owner="test-engineer", job="agent-decide",
        ),
    ]


def _all_catalog_rows() -> list[dict[str, Any]]:
    return [*_product_roles(), *_runtime_roles()]


def list_playbooks() -> list[dict[str, Any]]:
    return [
        {"id": "qa_tick", "label": "继续分析", "steps": [
            {"role": "req-analyst", "skill": "analyze_req"},
            {"role": "mindmap-writer", "skill": "draft_mindmap"},
            {"role": "case-writer", "skill": "draft_cases"},
        ]},
        {"id": "case_exec_agent", "label": "跑用例", "steps": [
            {"role": "test-engineer", "skill": "agent-decide"},
        ]},
        {"id": "settings_chat", "label": "设置页对话", "steps": []},
    ]


def list_roles() -> dict[str, Any]:
    from mino_nexus.ai.layer_stack import get_stack
    from mino_nexus.services.skill_store import list_skills

    product = [_apply_role_prompt(row) for row in _product_roles()]
    runtime = [_apply_role_prompt(row) for row in _runtime_roles()]
    abstract = [p for p in product if p.get("group") == "abstract"]
    workers = [p for p in product if p.get("group") != "abstract"]
    stack = get_stack()
    jobs = [dict(row) for row in (stack.get("skills") or [])]
    bound = {str(row.get("id") or ""): list(row.get("skill_ids") or []) for row in (stack.get("roles") or [])}
    for role in product:
        ids = bound.get(str(role.get("id") or ""), list(role.get("skill_ids") or []))
        role["skill_ids"] = ids
        role["skills"] = [row for row in jobs if row.get("id") in ids]
    trees = []
    for p in workers:
        caps = [s for s in jobs if s.get("id") in (p.get("skill_ids") or [])] or [
            r for r in runtime if r.get("owner") == p["id"]
        ]
        trees.append({**p, "capabilities": caps})
    owners = [{"id": t["id"], "label": t["label"], "role_ids": [t["id"], *[c["id"] for c in t.get("capabilities") or []]]} for t in trees]
    called_counts: dict[str, int] = {}
    for row in product:
        key = str(row.get("called") or "unknown")
        called_counts[key] = called_counts.get(key, 0) + 1
    skills = list_skills()
    return {
        "abstract": abstract,
        "product": product,
        "runtime": runtime,
        "meta": [],
        "jobs": jobs,
        "skills": skills,
        "skill_categories": list(stack.get("skill_categories") or []),
        "trees": trees,
        "roles": product,
        "owners": owners,
        "playbooks": list_playbooks(),
        "counts": {
            "product": len(workers),
            "abstract": len(abstract),
            "runtime": len(runtime),
            "meta": 0,
            "skills": len(skills),
            "jobs": len(jobs),
            "roles": len(product),
            "total": len(product),
            "called": called_counts,
        },
    }


def get_role(role_id: str) -> Optional[dict[str, Any]]:
    rid = str(role_id or "").strip()
    if not rid:
        return None
    for row in _all_catalog_rows():
        if row["id"] == rid:
            return _apply_role_prompt(row)
    try:
        from mino_nexus.services.skill_store import get_skill, skill_prompt

        skill = get_skill(rid)
        if skill:
            prompt = skill_prompt(rid)
            return {
                **skill,
                "kind": "json" if skill.get("engine") == "qa_job" else "conversational",
                "group": "product",
                "default_prompt": prompt,
                "system_prompt": prompt,
                "prompt_chars": len(prompt),
            }
    except Exception:
        pass
    return None


def _sanitize_messages(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        out.append({"role": role, "content": content[:_MAX_CONTENT]})
    return out[-_MAX_HISTORY:]


def chat_with_role(*, role_id: str, messages: Any, explain_mode: bool = False) -> dict[str, Any]:
    role = get_role(role_id)
    if not role:
        raise ValueError(f"未知角色：{role_id}")
    history = _sanitize_messages(messages)
    if not history or history[-1]["role"] != "user":
        raise ValueError("请先发送一条用户消息")

    from mino_nexus.ai.llm_client import call_chat_plain, parse_token_usage, resolve_regression_provider
    from mino_nexus.services.job_store import job_system_text, resolve_job_id

    provider, gate = resolve_regression_provider()
    if not provider:
        raise RuntimeError(
            "还没有配置可用的大模型。请到 Studio → 密钥 → 大模型 Key，打开「可用」并勾选用例。"
            + (f"（{gate.get('reason')}）" if gate.get("reason") else "")
        )
    jid = ROLE_JOB_MAP.get(role_id) or resolve_job_id(role_id)
    system = job_system_text(jid, explain_mode=explain_mode)
    if not system:
        system = str(role.get("system_prompt") or "").strip()
    payload = [{"role": "system", "content": system}, *history]
    reply, meta = call_chat_plain(
        provider=provider,
        messages=payload,
        temperature=0.4 if explain_mode or role.get("kind") == "conversational" else 0.15,
        max_tokens=2048,
        timeout_sec=90,
    )
    if not reply:
        raise RuntimeError(meta.get("error") or "模型没有返回内容")
    usage = parse_token_usage(meta.get("usage"))
    return {
        "role_id": role["id"],
        "role_label": role["label"],
        "reply": reply,
        "explain_mode": bool(explain_mode),
        "provider_id": meta.get("provider_id") or "",
        "model": meta.get("model") or "",
        "elapsed_ms": meta.get("elapsed_ms") or 0,
        "usage": usage,
    }
