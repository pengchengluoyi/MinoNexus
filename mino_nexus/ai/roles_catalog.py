"""角色目录：产品身份 + 仓库内 LLM system prompt。"""
from __future__ import annotations

from typing import Any, Optional

from mino_nexus.ai import prompts as P
from mino_nexus.ai.role_prompts import (
    CASE_WRITER_SYSTEM_PROMPT,
    DOC_KEEPER_SYSTEM_PROMPT,
    EXPLAIN_OVERLAY,
    KNOWLEDGE_REVIEWER_SYSTEM_PROMPT,
    MINDMAP_WRITER_SYSTEM_PROMPT,
    PRODUCT_EXPERT_SYSTEM_PROMPT,
    REPORT_WRITER_SYSTEM_PROMPT,
    REQ_ANALYST_IMPACT_PROMPT,
    REQ_ANALYST_SYSTEM_PROMPT,
    REQ_QA_BM_SYSTEM_PROMPT,
    TEST_ENGINEER_SYSTEM_PROMPT,
    VERSION_QA_BM_SYSTEM_PROMPT,
)

_EDITABLE_ROLE_IDS = {"im-qa-assistant", "im-defect-assistant"}
_MAX_HISTORY = 20
_MAX_CONTENT = 8000

CONDUCTOR_SYSTEM_PROMPT = """你是 Mino 的分析师。看当前步骤该调用哪个角色的哪项能力。默认走剧本，不每步再打一层模型。

不要假装已经操作了真机或改写了流程门禁。用中文说明下一步该谁做、做什么。"""

IM_DIALOGUE_PROMPT = """你是 Mino 的 IM 总指挥。在通道里回答测试同学的问题，并可建议推进流程、下发任务。人审门禁仍须人点。不要编造未提供的任务或设备。"""

IM_DEFECT_PROMPT = """你是 Mino 的 IM 缺陷助手。把说清的缺陷整理成 JSON：title、steps、expected、actual、severity。信息不够就列出还缺什么，不要假装已经建单。"""


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


def _apply_role_prompt(row: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.settings_store import get_role_prompt_override

    out = dict(row)
    built_in = str(out.get("default_prompt") or out.get("system_prompt") or "").strip()
    out["default_prompt"] = built_in
    override = get_role_prompt_override(str(out.get("id") or ""))
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
            source="mino_nexus/ai/roles_catalog.py",
            used_in=["route", "qa_tick", "case_exec_agent"],
            summary="抽象调度：看当前步骤该调用哪个角色的哪项能力。",
            system_prompt=CONDUCTOR_SYSTEM_PROMPT,
            related_ids=["req-analyst", "test-engineer", "req-qa-bm"],
            live=True, owner="conductor", job="route", called="wired",
            triggers=["流程 tick 选下一步", "设置页问「这一步该调谁」"],
        ),
        _role(
            id="req-analyst", label="需求分析师", group="product", kind="json",
            source="mino_nexus/ai/role_prompts.py",
            used_in=["analyze_req", "propose_atlas"],
            summary="读需求原文，拆验收标准和测试点。",
            system_prompt=REQ_ANALYST_SYSTEM_PROMPT,
            related_ids=["mindmap-writer", "case-writer", "req-qa-bm"],
            live=True, owner="req-analyst", job="analyze_req", called="wired",
            triggers=["新建需求", "流程 tick"],
        ),
        _role(
            id="mindmap-writer", label="测试脑图编写", group="product", kind="json",
            source="mino_nexus/ai/role_prompts.py",
            used_in=["draft_mindmap"],
            summary="沿入口和端铺覆盖脑图。",
            system_prompt=MINDMAP_WRITER_SYSTEM_PROMPT,
            related_ids=["req-analyst", "case-writer"],
            live=True, owner="mindmap-writer", job="draft_mindmap", called="wired",
            triggers=["需求分析完成", "流程 tick"],
        ),
        _role(
            id="case-writer", label="测试用例编写", group="product", kind="json",
            source="mino_nexus/ai/role_prompts.py",
            used_in=["draft_cases"],
            summary="按测试点展开用例草稿。",
            system_prompt=CASE_WRITER_SYSTEM_PROMPT,
            related_ids=["req-analyst", "mindmap-writer"],
            live=True, owner="case-writer", job="draft_cases", called="wired",
            triggers=["测试点缺用例", "流程 tick"],
        ),
        _role(
            id="req-qa-bm", label="需求QA BM", group="product", kind="conversational",
            source="mino_nexus/ai/role_prompts.py",
            used_in=["map_cases", "classify_fail", "draft_sign"],
            summary="一条需求从覆盖到验收。结论必须人审。",
            system_prompt=REQ_QA_BM_SYSTEM_PROMPT,
            related_ids=["req-analyst", "case-writer"],
            live=True, owner="req-qa-bm", job="draft_sign", called="wired",
        ),
        _role(
            id="version-qa-bm", label="版本QA BM", group="product", kind="conversational",
            source="mino_nexus/ai/role_prompts.py",
            used_in=["pick_regression", "draft_gate"],
            summary="这版带哪些需求出门、历史回归与发版评审。",
            system_prompt=VERSION_QA_BM_SYSTEM_PROMPT,
            related_ids=["req-qa-bm"],
            live=True, owner="version-qa-bm", job="draft_gate", called="wired",
        ),
        _role(
            id="test-engineer", label="测试工程师", group="product", kind="conversational",
            source="mino_nexus/ai/role_prompts.py",
            used_in=["用例执行", "Agent"],
            summary="在设备上执行用例并给出屏幕证据。",
            system_prompt=TEST_ENGINEER_SYSTEM_PROMPT,
            related_ids=["agent-decide", "plan-overview"],
            live=True, owner="test-engineer", called="wired",
            triggers=["下发任务", "Case Runner"],
        ),
        _role(
            id="report-writer", label="报告编写", group="product", kind="json",
            source="mino_nexus/ai/role_prompts.py",
            used_in=["测试报告"],
            summary="按流程事实写报告草稿。",
            system_prompt=REPORT_WRITER_SYSTEM_PROMPT,
            related_ids=["req-qa-bm", "doc-keeper"],
            live=True, owner="report-writer", job="draft_test_report", called="sandbox",
        ),
        _role(
            id="doc-keeper", label="文档维护", group="product", kind="json",
            source="mino_nexus/ai/role_prompts.py",
            used_in=["文档插件"],
            summary="把人确认过的内容同步到外部文档。",
            system_prompt=DOC_KEEPER_SYSTEM_PROMPT,
            related_ids=["report-writer"],
            live=True, owner="doc-keeper", job="publish_wiki", called="sandbox",
        ),
        _role(
            id="im-qa-assistant", label="IM 总指挥", group="product", kind="conversational",
            source="mino_nexus/ai/roles_catalog.py",
            used_in=["角色 · prompt"],
            summary="IM 通道里的总指挥。prompt 可在设置页改。",
            system_prompt=IM_DIALOGUE_PROMPT,
            related_ids=["im-defect-assistant"],
            live=True, owner="im-qa-assistant", called="wired",
        ),
        _role(
            id="im-defect-assistant", label="IM 缺陷助手", group="product", kind="json",
            source="mino_nexus/ai/roles_catalog.py",
            used_in=["IM 提缺陷"],
            summary="把缺陷整理成 JSON。prompt 可在设置页改。",
            system_prompt=IM_DEFECT_PROMPT,
            related_ids=["im-qa-assistant"],
            live=True, owner="im-defect-assistant", called="wired",
        ),
        _role(
            id="knowledge-reviewer", label="知识审核员", group="product", kind="json",
            source="mino_nexus/ai/role_prompts.py",
            used_in=["知识库机审"],
            summary="审执行沉淀的知识草稿。",
            system_prompt=KNOWLEDGE_REVIEWER_SYSTEM_PROMPT,
            related_ids=["product-expert"],
            live=True, owner="knowledge-reviewer", job="review_knowledge", called="wired",
        ),
        _role(
            id="product-expert", label="产品专家", group="product", kind="json",
            source="mino_nexus/ai/role_prompts.py",
            used_in=["应用简报"],
            summary="了解被测应用的图谱、功能和需求。",
            system_prompt=PRODUCT_EXPERT_SYSTEM_PROMPT,
            related_ids=["knowledge-reviewer", "req-analyst"],
            live=True, owner="product-expert", job="brief_knowledge", called="wired",
        ),
    ]


def _runtime_roles() -> list[dict[str, Any]]:
    return [
        _role(
            id="propose_atlas", label="影响范围与骨架变更", group="runtime", kind="json",
            source="mino_nexus/ai/role_prompts.py",
            used_in=["propose_atlas"],
            summary="判断需求影响哪些模块/功能。只出品变更，等人确认。",
            system_prompt=REQ_ANALYST_IMPACT_PROMPT,
            owner="req-analyst", job="propose_atlas",
        ),
        _role(
            id="agent-decide", label="看图决策", group="runtime", kind="json",
            source="mino_nexus/ai/prompts.py",
            used_in=["Agent 逐步执行"],
            summary="每一步看截图决定下一个动作。",
            system_prompt=getattr(P, "AGENT_DO_SYSTEM_PROMPT", "") or getattr(P, "AGENT_DECIDE_SYSTEM_PROMPT", ""),
            owner="test-engineer", job="agent-decide",
        ),
        _role(
            id="plan-overview", label="回归测试规划器", group="runtime", kind="json",
            source="mino_nexus/ai/prompts.py",
            used_in=["AI 回归规划"],
            summary="纯文本规划整条用例的事件序列。",
            system_prompt=P.PLAN_OVERVIEW_SYSTEM_PROMPT,
            owner="test-engineer", job="plan-overview",
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

    product = [_apply_role_prompt(row) for row in _product_roles()]
    runtime = [_apply_role_prompt(row) for row in _runtime_roles()]
    abstract = [p for p in product if p.get("group") == "abstract"]
    workers = [p for p in product if p.get("group") != "abstract"]
    stack = get_stack()
    skills = [dict(row) for row in (stack.get("skills") or [])]
    bound = {str(row.get("id") or ""): list(row.get("skill_ids") or []) for row in (stack.get("roles") or [])}
    for role in product:
        ids = bound.get(str(role.get("id") or ""), list(role.get("skill_ids") or []))
        role["skill_ids"] = ids
        role["skills"] = [row for row in skills if row.get("id") in ids]
    trees = []
    for p in workers:
        caps = [s for s in skills if s.get("id") in (p.get("skill_ids") or [])] or [
            r for r in runtime if r.get("owner") == p["id"]
        ]
        trees.append({**p, "capabilities": caps})
    owners = [{"id": t["id"], "label": t["label"], "role_ids": [t["id"], *[c["id"] for c in t.get("capabilities") or []]]} for t in trees]
    called_counts: dict[str, int] = {}
    for row in product:
        key = str(row.get("called") or "unknown")
        called_counts[key] = called_counts.get(key, 0) + 1
    return {
        "abstract": abstract,
        "product": product,
        "runtime": runtime,
        "meta": [],
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

    provider, gate = resolve_regression_provider()
    if not provider:
        raise RuntimeError(
            "还没有配置可用的大模型。请到 Studio → 密钥 → 大模型 Key，打开「可用」并勾选用例。"
            + (f"（{gate.get('reason')}）" if gate.get("reason") else "")
        )
    system = str(role.get("system_prompt") or "").strip()
    if explain_mode:
        system = f"{EXPLAIN_OVERLAY}\n\n{system}".strip()
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
