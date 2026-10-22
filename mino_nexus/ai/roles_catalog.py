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
    "report-writer": "draft_test_report",
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
    from mino_nexus.ai.job_store import job_system_text, resolve_job_id

    jid = ROLE_JOB_MAP.get(role_or_job_id) or resolve_job_id(role_or_job_id)
    return job_system_text(jid)


def _apply_role_prompt(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    rid = str(out.get("id") or "")
    built_in = _job_prompt(rid) or str(out.get("default_prompt") or out.get("system_prompt") or "").strip()
    out["default_prompt"] = built_in
    out["system_prompt"] = built_in
    out["prompt_chars"] = len(built_in)
    out["prompt_custom"] = False
    return out


def _roles_from_files(group: str) -> list[dict[str, Any]]:
    from mino_nexus.platform.llm.repo import role_specs

    rows: list[dict[str, Any]] = []
    for spec in role_specs():
        if group == "runtime" and spec.get("group") != "runtime":
            continue
        if group != "runtime" and spec.get("group") == "runtime":
            continue
        prompt = ""
        rows.append({
            "id": spec.get("id"),
            "label": spec.get("label") or spec.get("id"),
            "group": spec.get("group") or "",
            "kind": spec.get("kind") or "",
            "source": spec.get("source") or "",
            "used_in": list(spec.get("used_in") or []),
            "summary": spec.get("summary") or "",
            "system_prompt": prompt,
            "prompt_chars": 0,
            "related_ids": list(spec.get("related_ids") or []),
            "live": spec.get("live", True) is not False,
            "owner": spec.get("owner") or "",
            "job": spec.get("job") or "",
            "chat_jobs": list(spec.get("chat_jobs") or []),
            "called": spec.get("called") or "",
            "triggers": list(spec.get("triggers") or []),
            "output": spec.get("output") or "",
            "editable": False,
            "default_prompt": prompt,
            "prompt_custom": False,
            "version": int(spec.get("version") or 1),
            "sha": str(spec.get("_sha") or ""),
            "source_path": str(spec.get("_source_path") or ""),
        })
    return rows


def _product_roles() -> list[dict[str, Any]]:
    return _roles_from_files("product")


def _runtime_roles() -> list[dict[str, Any]]:
    return _roles_from_files("runtime")


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
    from mino_nexus.ai.skill_store import list_skills

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
        from mino_nexus.ai.skill_store import get_skill, skill_prompt

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


def chat_with_role(*, role_id: str, messages: Any, explain_mode: bool = False, job_id: str = "", timeout_sec: float = 90) -> dict[str, Any]:
    role = get_role(role_id)
    if not role:
        raise ValueError(f"未知角色：{role_id}")
    if job_id:
        from mino_nexus.platform.llm.repo import get_job
        spec = get_job(job_id)
        if job_id not in role.get("chat_jobs", []) or not spec or spec.get("engine") not in ("text_chat", "chat_reply"):
            raise ValueError("该角色不允许调用此对话 job")
    history = _sanitize_messages(messages)
    if not history or history[-1]["role"] != "user":
        raise ValueError("请先发送一条用户消息")

    from mino_nexus.platform.llm.client import call_chat_plain, parse_token_usage, resolve_regression_provider
    from mino_nexus.ai.job_store import job_system_text, resolve_job_id

    provider, gate = resolve_regression_provider()
    if not provider:
        raise RuntimeError(
            "还没有配置可用的大模型。请到 Studio → 密钥 → 大模型 Key，打开「可用」并勾选用例。"
            + (f"（{gate.get('reason')}）" if gate.get("reason") else "")
        )
    jid = job_id or ROLE_JOB_MAP.get(role_id) or resolve_job_id(role_id)
    system = job_system_text(jid, explain_mode=explain_mode)
    if not system:
        system = str(role.get("system_prompt") or "").strip()
    payload = [{"role": "system", "content": system}, *history]
    reply, meta = call_chat_plain(
        provider=provider,
        messages=payload,
        temperature=0.4 if explain_mode or role.get("kind") == "conversational" else 0.15,
        max_tokens=2048,
        timeout_sec=min(90, timeout_sec),
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
