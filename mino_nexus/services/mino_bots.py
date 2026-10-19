"""Mino Bots V7：组群、任务拆解、触发执行。"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from mino_nexus.core.log import SLog
from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem, ProjectBot

BOT_TEMPLATES: dict[str, dict[str, Any]] = {
    "collab-default": {
        "label": "协作助手",
        "description": "讨论区默认发言身份；测试交付编排统一走 qa_cover（与 QA 流程同源）",
        "job_ids": ["analyze_req"],
        "runtime_role": "display_only",
    },
    "test-focus": {
        "label": "测试助手",
        "description": "可选第二身份；不改变流水线，仅影响议题成员展示",
        "job_ids": ["analyze_req", "draft_cases"],
        "runtime_role": "display_only",
    },
    "delivery-pipeline": {
        "label": "交付流水线",
        "description": "与「运行测试分析」相同：LangGraph / AgentScope Team → qa_cover 各 job",
        "job_ids": ["analyze_req", "draft_mindmap", "draft_cases", "req-qa-bm", "knowledge-reviewer"],
        "pipeline": "langgraph_delivery",
        "runtime_role": "display_only",
    },
    "dev-openhands": {
        "label": "开发助手",
        "description": "配置 OpenHands 后，可把开发任务提交到开源代码代理",
        "job_ids": [],
        "action_kind": "openhands",
    },
    "review-panel": {
        "label": "评审组",
        "description": "需求与用例多角色评审",
        "job_ids": ["req-qa-bm", "knowledge-reviewer"],
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid(sess: dict[str, Any] | None) -> str:
    return str((sess or {}).get("user_id") or "").strip()


def _require_access(project_id: str, sess: dict[str, Any]) -> None:
    from mino_nexus.services.project_members import require_maintainer

    require_maintainer(project_id, sess)


def list_templates() -> list[dict[str, Any]]:
    return [
        {"id": k, **v, "install_once": True, "note": "每模板每项目只需一条记录；重复安装不会增加能力"}
        for k, v in BOT_TEMPLATES.items()
        if k != "dev-openhands"
    ]


def _marketplace_path():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "catalog" / "collab_marketplace.yaml"


def _installed_template_ids(project_id: str) -> set[str]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import ProjectBot

    with session_scope() as db:
        rows = (
            db.query(ProjectBot)
            .filter_by(project_id=project_id, status="active")
            .order_by(ProjectBot.created_at)
            .all()
        )
        seen: set[str] = set()
        for row in rows:
            seen.add(str(row.template_id or "collab-default"))
        return seen


def dedupe_project_bots(project_id: str, sess: dict[str, Any]) -> int:
    """同 template_id 只保留最早一条 active，其余标为 archived。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import ProjectBot

    _require_access(project_id, sess)
    archived = 0
    with session_scope() as db:
        rows = (
            db.query(ProjectBot)
            .filter_by(project_id=project_id, status="active")
            .order_by(ProjectBot.created_at)
            .all()
        )
        seen: set[str] = set()
        for row in rows:
            tid = str(row.template_id or "collab-default")
            if tid in seen:
                row.status = "archived"
                archived += 1
            else:
                seen.add(tid)
        db.flush()
    return archived


def list_marketplace(project_id: str | None = None, sess: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    import yaml

    from mino_nexus.services.collab_extended import merge_marketplace_items

    path = _marketplace_path()
    if not path.is_file():
        local = [{"id": k, "template_id": k, **v} for k, v in BOT_TEMPLATES.items()]
        return merge_marketplace_items(local)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = data.get("items") if isinstance(data, dict) else []
    out: list[dict[str, Any]] = []
    for row in items or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("template_id") or row.get("id") or "").strip()
        if tid == "dev-openhands":
            continue
        tpl = BOT_TEMPLATES.get(tid, {})
        item = {
            **row,
            "template_id": tid,
            "label": row.get("label") or tpl.get("label"),
            "description": row.get("description") or tpl.get("description", ""),
        }
        out.append(item)
    merged = merge_marketplace_items(out)
    installed: set[str] = set()
    if project_id and sess:
        try:
            installed = _installed_template_ids(project_id)
        except Exception:
            installed = set()
    for row in merged:
        tid = str(row.get("template_id") or row.get("id") or "")
        row["installed"] = tid in installed
        row["install_once"] = True
    return merged


def install_marketplace_bot(project_id: str, sess: dict[str, Any], *, market_id: str, name: str = "") -> dict[str, Any]:
    mid = str(market_id or "").strip()
    row = next((x for x in list_marketplace() if str(x.get("id") or "") == mid), None)
    if row is None:
        raise ValueError("unknown_market_item")
    tid = str(row.get("template_id") or mid)
    label = str(name or "").strip() or str(row.get("label") or BOT_TEMPLATES.get(tid, {}).get("label") or tid)
    return create_bot(project_id, sess, name=label, template_id=tid)


def _bot_public(row: ProjectBot) -> dict[str, Any]:
    return {
        "bot_id": row.bot_id,
        "project_id": row.project_id,
        "name": row.name or "",
        "template_id": row.template_id or "collab-default",
        "app_ids": list(row.app_ids or []),
        "platforms": list(row.platforms or []) if isinstance(row.platforms, list) else [],
        "policy_override": dict(row.policy_override or {}) if isinstance(row.policy_override, dict) else {},
        "status": row.status or "active",
        "config_revision": int(row.config_revision or 1),
        "created_at": row.created_at or "",
    }


def list_bots(project_id: str, sess: dict[str, Any]) -> list[dict[str, Any]]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import ProjectBot

    _require_access(project_id, sess)
    dedupe_project_bots(project_id, sess)
    with session_scope() as db:
        rows = (
            db.query(ProjectBot)
            .filter_by(project_id=project_id, status="active")
            .order_by(ProjectBot.created_at)
            .all()
        )
        return [_bot_public(r) for r in rows]


def create_bot(
    project_id: str,
    sess: dict[str, Any],
    *,
    name: str,
    template_id: str = "collab-default",
    app_ids: list[str] | None = None,
    platforms: list[str] | None = None,
    policy_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import ProjectBot

    _require_access(project_id, sess)
    tid = str(template_id or "collab-default").strip()
    if tid not in BOT_TEMPLATES:
        raise ValueError("unknown_template")
    label = str(name or "").strip() or BOT_TEMPLATES[tid]["label"]
    with session_scope() as db:
        existing = (
            db.query(ProjectBot)
            .filter_by(project_id=project_id, template_id=tid, status="active")
            .order_by(ProjectBot.created_at)
            .first()
        )
        if existing is not None:
            if label and label != str(existing.name or ""):
                existing.name = label
            if platforms is not None:
                existing.platforms = list(platforms)
            if policy_override is not None:
                existing.policy_override = dict(policy_override)
            db.flush()
            out = _bot_public(existing)
            out["already_exists"] = True
            return out
        row = ProjectBot(
            bot_id=str(uuid.uuid4()),
            project_id=project_id,
            name=label,
            template_id=tid,
            app_ids=list(app_ids or []),
            platforms=list(platforms or []),
            policy_override=dict(policy_override or {}),
            status="active",
            config_revision=1,
            created_at=_now(),
        )
        db.add(row)
        db.flush()
        return _bot_public(row)


def patch_bot(
    project_id: str,
    bot_id: str,
    sess: dict[str, Any],
    *,
    name: str | None = None,
    app_ids: list[str] | None = None,
    platforms: list[str] | None = None,
    policy_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import ProjectBot

    _require_access(project_id, sess)
    bid = str(bot_id or "").strip()
    with session_scope() as db:
        row = db.get(ProjectBot, bid)
        if row is None or row.project_id != project_id:
            raise KeyError("bot_not_found")
        if name is not None:
            row.name = str(name or "").strip() or row.name
        if app_ids is not None:
            row.app_ids = list(app_ids)
        if platforms is not None:
            row.platforms = list(platforms)
        if policy_override is not None:
            row.policy_override = dict(policy_override)
        db.flush()
        return _bot_public(row)


def ensure_default_bots(project_id: str, sess: dict[str, Any]) -> list[dict[str, Any]]:
    dedupe_project_bots(project_id, sess)
    bots = list_bots(project_id, sess)
    have = {str(b.get("template_id") or "") for b in bots}
    if "collab-default" not in have:
        create_bot(project_id, sess, name=BOT_TEMPLATES["collab-default"]["label"], template_id="collab-default")
    if "delivery-pipeline" not in have:
        create_bot(
            project_id,
            sess,
            name=BOT_TEMPLATES["delivery-pipeline"]["label"],
            template_id="delivery-pipeline",
        )
    return list_bots(project_id, sess)


def _agent_canvas_public(pipeline_state: dict[str, Any]) -> dict[str, Any]:
    canvas = pipeline_state.get("agent_canvas")
    return dict(canvas) if isinstance(canvas, dict) else {}


def collab_surface_guide(topic_row: CollabTopic | dict[str, Any]) -> dict[str, Any]:
    """兼容旧客户端；测试管家 UI 不再展示大段说明。"""
    return {"mode": "steward", "hint": "在聊天里说「现在怎么样了」或「跑吧」。"}


def _topic_public(row: CollabTopic, members: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "topic_id": row.topic_id,
        "project_id": row.project_id,
        "app_id": row.app_id or "",
        "title": row.title or "",
        "goal": row.goal or "",
        "status": row.status or "open",
        "lead_user_id": row.lead_user_id or "",
        "input_version": row.input_version or "v1",
        "anchor": dict(row.anchor or {}) if isinstance(row.anchor, dict) else {},
        "memory": dict(row.memory or {}) if isinstance(row.memory, dict) else {},
        "pipeline_state": dict(row.pipeline_state or {}) if isinstance(row.pipeline_state, dict) else {},
        "agent_canvas": _agent_canvas_public(row.pipeline_state if isinstance(row.pipeline_state, dict) else {}),
        "im_route": dict(row.im_route or {}) if isinstance(row.im_route, dict) else {},
        "knowledge_graph": dict(row.knowledge_graph or {}) if isinstance(row.knowledge_graph, dict) else {},
        "digest": dict(row.digest or {}) if isinstance(row.digest, dict) else {},
        "paused": bool(int(row.paused or 0)),
        "requirement_id": str(row.requirement_id or ""),
        "modules": list(row.modules or []) if isinstance(row.modules, list) else [],
        "scope_case_ids": list(row.scope_case_ids or []) if isinstance(row.scope_case_ids, list) else [],
        "env_profile": str(row.env_profile or ""),
        "env_surface": str(row.env_surface or ""),
        "platform": str(row.platform or ""),
        "expected_build": str(row.expected_build or ""),
        "scope_snapshot": dict(row.scope_snapshot or {}) if isinstance(row.scope_snapshot, dict) else {},
        "created_at": row.created_at or "",
        "updated_at": row.updated_at or "",
        "members": members or [],
        "guide": collab_surface_guide(row),
    }


def _load_topic_members(db, topic_id: str) -> list[dict[str, Any]]:
    from mino_nexus.models.mino_bots import CollabTopicMember, ProjectBot
    from mino_nexus.services.auth_store import public_user_by_id

    out: list[dict[str, Any]] = []
    for r in db.query(CollabTopicMember).filter_by(topic_id=topic_id).all():
        label = str(r.member_id or "")
        if r.member_kind == "bot":
            bot = db.get(ProjectBot, r.member_id)
            if bot and bot.name:
                label = str(bot.name)
        elif r.member_kind == "user":
            user = public_user_by_id(r.member_id)
            if user:
                label = str(user.get("name") or user.get("username") or label)
        out.append(
            {
                "kind": r.member_kind,
                "id": r.member_id,
                "label": label,
                "joined_at": r.joined_at or "",
            }
        )
    return out


def _next_input_version(ver: str) -> str:
    text = str(ver or "v1").strip() or "v1"
    m = re.match(r"^v(\d+)$", text, re.I)
    if m:
        return f"v{int(m.group(1)) + 1}"
    return f"{text}-2"


def _discussion_text(db, topic_id: str, limit: int = 20) -> str:
    from mino_nexus.models.mino_bots import CollabMessage

    rows = (
        db.query(CollabMessage)
        .filter_by(topic_id=topic_id)
        .order_by(CollabMessage.created_at.desc())
        .limit(max(1, min(limit, 50)))
        .all()
    )
    rows.reverse()
    lines: list[str] = []
    for row in rows:
        who = "成员" if str(row.author_kind or "") == "user" else "Bot"
        body = str(row.body or "").strip()
        if body:
            lines.append(f"[{who}] {body}")
    return "\n".join(lines)


def _qa_tick_spec(topic: CollabTopic) -> dict[str, Any] | None:
    app_id = str(topic.app_id or "").strip()
    if not app_id:
        return None
    return {
        "title": "推进 QA 流程",
        "goal": "将议题上下文同步到应用 qa_process 并后台 tick（analyze/mindmap/draft）",
        "action_kind": "qa_tick",
        "action_payload": {"app_id": app_id},
        "acceptance": "qa_process job 完成且无 error 动作",
    }


def _case_run_spec_from_topic(topic: CollabTopic) -> dict[str, Any] | None:
    anchor = topic.anchor if isinstance(topic.anchor, dict) else {}
    if str(anchor.get("kind") or "") != "case":
        return None
    case_id = str(anchor.get("case_id") or "").strip()
    app_id = str(anchor.get("app_id") or topic.app_id or "").strip()
    if not case_id or not app_id:
        return None
    title = str(anchor.get("case_title") or case_id).strip()
    return {
        "title": "派发用例执行",
        "goal": f"在编译与绑定检查通过后执行用例：{title}（{case_id}）",
        "acceptance": "preflight 通过且已创建执行批次",
        "action_kind": "case_run",
        "action_payload": {"app_id": app_id, "case_ids": [case_id], "async_exec": True},
    }


def _append_bot_message(db, topic_id: str, body: str, *, bot_id: str = "", now: str | None = None) -> None:
    from mino_nexus.models.mino_bots import CollabMessage, CollabTopic

    text = str(body or "").strip()
    if not text:
        return
    stamp = now or _now()
    topic = db.get(CollabTopic, topic_id)
    if topic is None:
        return
    db.add(
        CollabMessage(
            message_id=str(uuid.uuid4()),
            topic_id=topic_id,
            author_kind="bot",
            author_id=str(bot_id or "collab-bot"),
            acting_bot_id=str(bot_id or ""),
            body=text,
            created_at=stamp,
        )
    )
    topic.updated_at = stamp


def _topic_list_item(row: CollabTopic) -> dict[str, Any]:
    """侧栏列表用轻量字段，避免 pipeline_state / members 等大 JSON。"""
    return {
        "topic_id": row.topic_id,
        "project_id": row.project_id,
        "app_id": row.app_id or "",
        "title": row.title or "",
        "goal": row.goal or "",
        "status": row.status or "open",
        "paused": bool(int(row.paused or 0)),
        "anchor": dict(row.anchor or {}) if isinstance(row.anchor, dict) else {},
        "requirement_id": str(row.requirement_id or ""),
        "modules": list(row.modules or []) if isinstance(row.modules, list) else [],
        "scope_case_ids": list(row.scope_case_ids or []) if isinstance(row.scope_case_ids, list) else [],
        "scope_snapshot": dict(row.scope_snapshot or {}) if isinstance(row.scope_snapshot, dict) else {},
        "updated_at": row.updated_at or "",
        "created_at": row.created_at or "",
        "guide": collab_surface_guide(row),
    }


def list_topics(project_id: str, sess: dict[str, Any]) -> list[dict[str, Any]]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic

    _require_access(project_id, sess)
    with session_scope() as db:
        rows = db.query(CollabTopic).filter_by(project_id=project_id).order_by(CollabTopic.updated_at.desc()).all()
        return [_topic_list_item(row) for row in rows]


def create_topic(
    project_id: str,
    sess: dict[str, Any],
    *,
    title: str,
    goal: str = "",
    app_id: str = "",
    bot_ids: list[str] | None = None,
    anchor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_access(project_id, sess)
    raise ValueError('请通过工作区入口选择项目需求或模块；单条用例请加入已有工作区')










def _anchor_matches(anchor: dict[str, Any], kind: str, case_id: str, app_id: str) -> bool:
    if str(anchor.get("kind") or "") != kind:
        return False
    if str(anchor.get("case_id") or "") != case_id:
        return False
    bound_app = str(anchor.get("app_id") or "").strip()
    if bound_app and app_id and bound_app != app_id:
        return False
    return True


def _case_excerpt(case: dict[str, Any], limit: int = 400) -> str:
    raw = case.get("steps_raw") or ""
    if not raw and isinstance(case.get("steps"), list):
        raw = "\n".join(str(x) for x in case.get("steps") or [])
    parts = [line.strip() for line in str(raw).splitlines() if line.strip()][:6]
    text = "；".join(parts)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def open_topic_for_case(
    project_id: str,
    sess: dict[str, Any],
    *,
    app_id: str,
    case_id: str,
) -> dict[str, Any]:
    _require_access(project_id, sess)
    raise ValueError('请通过工作区入口选择项目需求或模块；单条用例请加入已有工作区')










_CASE_ID_LINE = re.compile(r"编号[：:]\s*(case-[a-f0-9]+)", re.I)
_CASE_ID_TOKEN = re.compile(r"\b(case-[a-f0-9]{8,})\b", re.I)


def repair_topic_case_anchor(project_id: str, topic_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    """从议题 anchor / 讨论首条用例页镜像 / 工作项 payload 补全用例锚点。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabMessage, CollabTopic, CollabWorkItem
    from mino_nexus.services.case_store import get_case

    _require_access(project_id, sess)
    case_id = ""
    app_id = ""
    case_title = ""
    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None or topic.project_id != project_id:
            return {}
        anchor = dict(topic.anchor or {}) if isinstance(topic.anchor, dict) else {}
        if str(anchor.get("kind") or "") == "case":
            case_id = str(anchor.get("case_id") or "").strip()
            app_id = str(anchor.get("app_id") or topic.app_id or "").strip()
            case_title = str(anchor.get("case_title") or case_id).strip()
        if not case_id:
            rows = (
                db.query(CollabMessage)
                .filter_by(topic_id=topic_id)
                .order_by(CollabMessage.created_at)
                .all()
            )
            for row in rows:
                body = str(row.body or "")
                m = _CASE_ID_LINE.search(body) or _CASE_ID_TOKEN.search(body)
                if m:
                    case_id = m.group(1)
                    break
        if not case_id:
            for wi in db.query(CollabWorkItem).filter_by(topic_id=topic_id).all():
                payload = wi.action_payload if isinstance(wi.action_payload, dict) else {}
                ids = [str(x).strip() for x in (payload.get("case_ids") or []) if str(x).strip()]
                if ids:
                    case_id = ids[0]
                    break
                last = wi.last_result if isinstance(wi.last_result, dict) else {}
                for art in (payload.get("artifact"), last.get("artifact")):
                    if not isinstance(art, dict):
                        continue
                    inner = art.get("payload") if isinstance(art.get("payload"), dict) else art
                    for row in inner.get("cases") or inner.get("draft_cases") or []:
                        if not isinstance(row, dict):
                            continue
                        cid = str(row.get("case_id") or row.get("id") or "").strip()
                        if cid.startswith("case-"):
                            case_id = cid
                            break
                    if case_id:
                        break
                if case_id:
                    break
        if not case_id:
            return {}
        if not app_id:
            app_id = str(topic.app_id or "").strip()
        case = get_case(project_id, case_id)
        if case:
            case_title = str(case.get("title") or case.get("name") or case_title or case_id).strip()
        if not app_id:
            return {"kind": "case", "case_id": case_id, "case_title": case_title}
        new_anchor = {
            "kind": "case",
            "case_id": case_id,
            "app_id": app_id,
            "case_title": case_title or case_id,
        }
        topic.anchor = new_anchor
        if not str(topic.app_id or "").strip():
            topic.app_id = app_id
        topic.updated_at = _now()
        db.flush()
        return new_anchor


def _draft_case_ids_from_work_items(items: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for row in items:
        if str(row.get("action_kind") or "") != "apply_drafts":
            continue
        payload = row.get("action_payload") if isinstance(row.get("action_payload"), dict) else {}
        last = row.get("last_result") if isinstance(row.get("last_result"), dict) else {}
        for blob in (payload.get("artifact"), last.get("artifact")):
            if not isinstance(blob, dict):
                continue
            inner = blob.get("payload") if isinstance(blob.get("payload"), dict) else blob
            if not isinstance(inner, dict):
                continue
            for c in inner.get("cases") or inner.get("draft_cases") or []:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("case_id") or c.get("id") or "").strip()
                if cid.startswith("case-") and cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
    return ids


def _resolve_topic_app_id(project_id: str, topic: dict[str, Any]) -> str:
    app_id = str(topic.get("app_id") or "").strip()
    if app_id:
        return app_id
    from mino_nexus.services import project_store as ps

    try:
        apps = ps.list_apps(project_id)
    except Exception:
        apps = []
    if len(apps) == 1:
        return str(apps[0].get("id") or "")
    return ""


def ensure_topic_execution_anchor(project_id: str, topic_id: str, sess: dict[str, Any]) -> bool:
    """需求议题：用候选/已写入用例补全执行锚点（不靠用户去用例页点链接）。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic
    from mino_nexus.services.case_store import get_case

    repair_topic_case_anchor(project_id, topic_id, sess)
    topic = get_topic(project_id, topic_id, sess)
    anchor = topic.get("anchor") if isinstance(topic.get("anchor"), dict) else {}
    case_id = str(anchor.get("case_id") or "").strip()
    app_id = str(anchor.get("app_id") or topic.get("app_id") or "").strip()
    if case_id and app_id:
        return True
    if not case_id:
        found = _draft_case_ids_from_work_items(list_work_items(project_id, topic_id, sess))
        if found:
            case_id = found[0]
    if not case_id:
        return False
    if not app_id:
        app_id = _resolve_topic_app_id(project_id, topic)
    if not app_id:
        return False
    case = get_case(project_id, case_id)
    title = str((case or {}).get("title") or (case or {}).get("name") or case_id).strip()
    with session_scope() as db:
        row = db.get(CollabTopic, topic_id)
        if row is None:
            return False
        row.anchor = {
            "kind": "case",
            "case_id": case_id,
            "app_id": app_id,
            "case_title": title,
        }
        if not str(row.app_id or "").strip():
            row.app_id = app_id
        row.updated_at = _now()
        db.flush()
    return True


def run_testing_closure(project_id: str, topic_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    """测试管家：圈定 → 检查 → 按策略派发跑批。"""
    from mino_nexus.services.collab_steward import start_steward_flow

    return start_steward_flow(project_id, topic_id, sess)


def materialize_case_run_work_item(
    project_id: str, topic_id: str, sess: dict[str, Any]
) -> dict[str, Any] | None:
    """自主挂上「用例执行」工作项（不反问用户要哪条用例）。"""
    repair_topic_case_anchor(project_id, topic_id, sess)
    ensure_case_anchor_work_items(project_id, topic_id, sess)
    items = list_work_items(project_id, topic_id, sess)
    wi = next((i for i in items if str(i.get("action_kind")) == "case_run"), None)
    if wi is not None:
        return wi
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic

    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None:
            return None
        spec = _case_run_spec_from_topic(topic)
    if not spec:
        return None
    spec = dict(spec)
    spec["_slot"] = "case_run"
    sync_work_specs(
        project_id,
        topic_id,
        sess,
        [spec],
        idempotency_suffix="case_anchor",
        bump_version=False,
    )
    items = list_work_items(project_id, topic_id, sess)
    return next((i for i in items if str(i.get("action_kind")) == "case_run"), None)


def ensure_case_anchor_work_items(project_id: str, topic_id: str, sess: dict[str, Any]) -> None:
    """锚定用例的议题确保有「派发用例执行」与可选正式发布工作项。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem
    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None or topic.project_id != project_id:
            return
        spec = _case_run_spec_from_topic(topic)
        if not spec:
            return
        rows = db.query(CollabWorkItem).filter_by(topic_id=topic_id).all()
        kinds = {str(r.action_kind or "") for r in rows}
        to_add: list[dict[str, Any]] = []
        if "case_run" not in kinds:
            spec = dict(spec)
            spec.setdefault("origin", "case")
            to_add.append(spec)
    if not to_add:
        return
    try:
        decompose_topic(
            project_id,
            topic_id,
            sess,
            items=to_add,
            use_llm=False,
            include_case_run=False,
            idempotency_suffix="case_anchor",
        )
    except ValueError as exc:
        SLog.w("MinoBots", f"ensure_case_anchor_work_items: {exc}")


def update_topic(
    project_id: str,
    topic_id: str,
    sess: dict[str, Any],
    *,
    title: str | None = None,
    goal: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic

    _require_access(project_id, sess)
    now = _now()
    with session_scope() as db:
        row = db.get(CollabTopic, topic_id)
        if row is None or row.project_id != project_id:
            raise KeyError("topic_not_found")
        if title is not None:
            row.title = str(title or "").strip() or row.title
        if goal is not None:
            row.goal = str(goal or "").strip()
        if status is not None:
            row.status = str(status or "").strip() or row.status
        row.updated_at = now
        db.flush()
        return _topic_public(row, _load_topic_members(db, topic_id))


def get_agent_canvas(project_id: str, topic_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    topic = get_topic(project_id, topic_id, sess)
    return dict(topic.get("agent_canvas") or {})


def get_topic(project_id: str, topic_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem

    _require_access(project_id, sess)
    with session_scope() as db:
        row = db.get(CollabTopic, topic_id)
        if row is None or row.project_id != project_id:
            raise KeyError("topic_not_found")
        from mino_nexus.services.collab_steward import steward_overview, work_items_for_ui

        pub = _topic_public(row, _load_topic_members(db, topic_id))
        wi_rows = db.query(CollabWorkItem).filter_by(topic_id=topic_id).order_by(CollabWorkItem.created_at).all()
        items = work_items_for_ui([_work_item_public(r) for r in wi_rows])
        pub["overview"] = steward_overview(pub, items)
        return pub


def post_message(
    project_id: str,
    topic_id: str,
    sess: dict[str, Any],
    *,
    body: str,
    acting_bot_id: str = "",
    auto_orchestrate: bool = True,
    defer_orchestration: bool = False,
) -> dict[str, Any]:
    from mino_nexus.services.collab_chat import chat
    return chat(project_id, topic_id, sess, body)










def list_messages(project_id: str, topic_id: str, sess: dict[str, Any], limit: int = 100, *, through_message_id: str = "") -> list[dict[str, Any]]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabMessage, CollabTopic

    _require_access(project_id, sess)
    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        query = db.query(CollabMessage).filter_by(topic_id=topic_id)
        if through_message_id:
            source = db.get(CollabMessage, through_message_id)
            if source is None or source.topic_id != topic_id:
                raise KeyError("message_not_found")
            query = query.filter(CollabMessage.created_at <= source.created_at)
        rows = (
            query
            .order_by(CollabMessage.created_at.desc())
            .limit(max(1, min(limit, 500)))
            .all()
        )
        rows.reverse()
        from mino_nexus.services.collab_steward import filter_messages_for_ui

        from mino_nexus.models.collab_message_job import CollabMessageJob
        jobs = {j.message_id: j for j in db.query(CollabMessageJob).filter(
            CollabMessageJob.message_id.in_([r.message_id for r in rows])).all()} if rows else {}
        raw = [
            {
                "message_id": r.message_id,
                "reply_status": jobs[r.message_id].status if r.message_id in jobs else "",
                "reply_error": jobs[r.message_id].error if r.message_id in jobs else "",
                "author_kind": r.author_kind,
                "author_id": r.author_id,
                "acting_bot_id": r.acting_bot_id or "",
                "body": r.body or "",
                "created_at": r.created_at or "",
            }
            for r in rows
        ]
        return filter_messages_for_ui(raw)


_WORK_ITEM_KIND_UI: dict[str, dict[str, str]] = {
    "scope": {"label": "圈定与检查", "hint": "统计可执行与阻塞原因"},
    "run": {"label": "跑批", "hint": "真实 run，进度来自 run_store"},
    "recheck": {"label": "复验", "hint": "修复后重跑"},
    "issue": {"label": "问题项", "hint": "需人工处理"},
    "llm_job": {"label": "测试分析", "hint": "遗留，已停用"},
    "case_run": {"label": "用例执行", "hint": "preflight 通过后自动跑批，等待证据"},
    "apply_drafts": {"label": "写入用例草稿", "hint": "校验去重后自动写入"},
    "compile": {"label": "编译用例", "hint": "排队编译并等待终态"},
    "diagnose": {"label": "失败诊断", "hint": "分类并派生缺陷或重跑"},
    "defect": {"label": "缺陷", "hint": "业务失败，需人工修复"},
    "resource_gap": {"label": "资源缺口", "hint": "配置或设备缺口"},
    "publish": {"label": "正式发布", "hint": "策略开启时自动发布"},
    "v5_publish": {"label": "正式发布用例", "hint": "兼容旧项"},
    "qa_tick": {"label": "QA 推进", "hint": "遗留项，由 worker 跳过"},
    "openhands": {"label": "开发代理", "hint": "可选 OpenHands，非测试主线"},
    "none": {"label": "待办", "hint": ""},
}


def _work_item_public(row: CollabWorkItem) -> dict[str, Any]:
    kind = str(row.action_kind or "none")
    ui = _WORK_ITEM_KIND_UI.get(kind, {"label": kind, "hint": ""})
    return {
        "work_item_id": row.work_item_id,
        "topic_id": row.topic_id,
        "task_id": row.task_id or "",
        "title": row.title or "",
        "goal": row.goal or "",
        "acceptance": row.acceptance or "",
        "assignee_kind": row.assignee_kind or "bot",
        "assignee_id": row.assignee_id or "",
        "holder_user_id": row.holder_user_id or "",
        "input_version": row.input_version or "",
        "status": row.status or "pending",
        "action_kind": kind,
        "action_label": ui["label"],
        "action_hint": ui["hint"],
        "action_payload": dict(row.action_payload or {}),
        "last_result": dict(row.last_result or {}),
        "blocked_reason": row.blocked_reason or "",
        "depends_on": list(row.depends_on or []) if isinstance(row.depends_on, list) else [],
        "run_id": row.run_id or "",
        "case_ids": list(row.case_ids or []) if isinstance(row.case_ids, list) else [],
        "evidence": dict(row.evidence or {}),
        "attempt": int(row.attempt or 0),
        "next_check_at": row.next_check_at or "",
        "origin": row.origin or "",
        "activity": row.activity or "",
        "stage": row.stage or "",
        "heartbeat_at": row.heartbeat_at or "",
        "started_at": row.started_at or "",
        "created_at": row.created_at or "",
        "updated_at": row.updated_at or "",
    }


def list_work_items(project_id: str, topic_id: str, sess: dict[str, Any]) -> list[dict[str, Any]]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem

    _require_access(project_id, sess)
    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        from mino_nexus.services.collab_steward import work_items_for_ui

        rows = db.query(CollabWorkItem).filter_by(topic_id=topic_id).order_by(CollabWorkItem.created_at).all()
        return work_items_for_ui([_work_item_public(r) for r in rows])


def _idempotency_key(topic_id: str, input_version: str, goal: str, suffix: str = "") -> str:
    raw = f"{topic_id}\n{input_version}\n{suffix}\n{goal.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _spec_slot(spec: dict[str, Any], index: int) -> str:
    slot = str(spec.get("_slot") or "").strip()
    if slot:
        return slot
    kind = str(spec.get("action_kind") or "none")
    title = str(spec.get("title") or f"item-{index}")
    return f"{kind}:{title[:80]}"


def sync_work_specs(
    project_id: str,
    topic_id: str,
    sess: dict[str, Any],
    specs: list[dict[str, Any]],
    *,
    input_version: str = "",
    goal: str = "",
    idempotency_suffix: str = "pipeline",
    bump_version: bool = False,
) -> dict[str, Any]:
    """按 slot 幂等写入工作项：重复推进时更新草稿/结论，不堆叠同名项。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem, ProjectBot
    from mino_nexus.models.task import Task

    _require_access(project_id, sess)
    uid = _uid(sess)
    now = _now()
    cleaned = [dict(s) for s in specs if isinstance(s, dict) and str(s.get("title") or s.get("goal") or "").strip()]
    if not cleaned:
        raise ValueError("nothing_to_decompose")

    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        ver = str(input_version or topic.input_version or "v1").strip()
        if bump_version:
            ver = _next_input_version(ver)
            topic.input_version = ver
        use_goal = str(goal or topic.goal or "").strip()
        if goal:
            topic.goal = use_goal
        bot = db.query(ProjectBot).filter_by(project_id=project_id, status="active").first()
        assignee_id = bot.bot_id if bot else ""
        suffix = str(idempotency_suffix or "pipeline")
        touched: list[dict[str, Any]] = []
        created_count = 0
        slot_to_id: dict[str, str] = {}

        for idx, spec in enumerate(cleaned):
            analyze_done = bool(spec.pop("_analyze_done", False))
            embedded_art = spec.pop("_artifact", None)
            preset_status = str(spec.pop("status", "") or "").strip()
            title = str(spec.get("title") or f"工作项 {idx + 1}").strip()
            wi_goal = str(spec.get("goal") or title).strip()
            acceptance = str(spec.get("acceptance") or "").strip()
            action_kind = str(spec.get("action_kind") or "none").strip()
            payload = dict(spec.get("action_payload") or {})
            slot = _spec_slot(spec, idx)
            ikey = _idempotency_key(topic_id, ver, "", f"{suffix}:{slot}")
            origin = str(spec.get("origin") or idempotency_suffix or "pipeline")
            dep_slots = spec.get("depends_on_slots") if isinstance(spec.get("depends_on_slots"), list) else []
            depends_on = [str(x) for x in (spec.get("depends_on") or []) if str(x).strip()]
            for ds in dep_slots:
                wid = slot_to_id.get(str(ds))
                if wid:
                    depends_on.append(wid)

            existing = db.query(CollabWorkItem).filter_by(topic_id=topic_id, idempotency_key=ikey).first()
            initial_result: dict[str, Any] = {}
            if embedded_art is not None:
                initial_result = {"status": "ok", "reason_code": "pipeline_seed", "artifact": embedded_art}
            if analyze_done and embedded_art is not None:
                preset_status = preset_status or "done"
                initial_result = {
                    "status": "ok",
                    "reason_code": "analyze_seed",
                    "message": "流水线已完成分析",
                    "artifact": embedded_art,
                }
            if action_kind == "apply_drafts" and embedded_art is not None and not initial_result:
                initial_result = {"status": "ok", "reason_code": "draft_candidate", "artifact": embedded_art}

            if existing is not None:
                if str(existing.status or "") not in ("running", "waiting"):
                    existing.title = title
                    existing.goal = wi_goal
                    existing.acceptance = acceptance
                    existing.action_kind = action_kind
                    existing.action_payload = payload
                    if depends_on:
                        existing.depends_on = depends_on
                    if origin:
                        existing.origin = origin
                    if initial_result:
                        existing.last_result = initial_result
                    if preset_status:
                        existing.status = preset_status
                    elif str(existing.status or "") in ("done", "failed") and action_kind in (
                        "apply_drafts",
                        "compile",
                        "case_run",
                        "llm_job",
                    ):
                        existing.status = "pending"
                    existing.updated_at = now
                slot_to_id[slot] = existing.work_item_id
                touched.append(_work_item_public(existing))
                continue

            task_row = Task(
                id=str(uuid.uuid4()),
                app_id=str(topic.app_id or ""),
                name=title[:200],
                task_type="collab_work_item",
                status="pending",
                created_at=now,
            )
            db.add(task_row)
            initial_status = preset_status or "pending"
            wi = CollabWorkItem(
                work_item_id=str(uuid.uuid4()),
                topic_id=topic_id,
                task_id=task_row.id,
                title=title,
                goal=wi_goal,
                acceptance=acceptance,
                assignee_kind=str(spec.get("assignee_kind") or "bot"),
                assignee_id=str(spec.get("assignee_id") or assignee_id),
                holder_user_id=uid,
                input_version=ver,
                idempotency_key=ikey,
                status=initial_status,
                action_kind=action_kind,
                action_payload=payload,
                last_result=initial_result,
                depends_on=depends_on,
                origin=origin,
                created_at=now,
                updated_at=now,
            )
            slot_to_id[slot] = wi.work_item_id
            if initial_status == "done":
                task_row.status = "done"
            db.add(wi)
            db.flush()
            from mino_nexus.services.collab_extended import ensure_work_item_approval

            ensure_work_item_approval(
                db,
                work_item_id=wi.work_item_id,
                topic_id=topic_id,
                action_kind=action_kind,
                payload=payload,
                requester_id=uid,
            )
            created_count += 1
            touched.append(_work_item_public(wi))

        topic.input_version = ver
        topic.updated_at = now
        if created_count:
            titles = "、".join(item["title"] for item in touched[:6])
            _append_bot_message(
                db,
                topic_id,
                f"已同步 {len(touched)} 个工作项（新建 {created_count}）：{titles}。",
                bot_id=assignee_id,
                now=now,
            )
        db.flush()
    try:
        from mino_nexus.services.collab_worker import wake

        wake()
    except Exception:
        pass
    return {"idempotent": created_count == 0, "input_version": ver, "items": touched}


def _split_goal_lines(goal: str) -> list[str]:
    parts: list[str] = []
    for line in re.split(r"[\n\r]+", goal or ""):
        line = re.sub(r"^[\s\-*\d.)、]+", "", line.strip())
        if line:
            parts.append(line)
    if not parts and (goal or "").strip():
        parts.append(goal.strip())
    return parts[:12]


def _line_specs(goal: str, default_action_kind: str, default_job: str) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for line in _split_goal_lines(goal):
        spec: dict[str, Any] = {"title": line, "goal": line, "action_kind": default_action_kind}
        if default_action_kind == "llm_job":
            spec["action_payload"] = {"job": default_job}
        specs.append(spec)
    return specs


def _analyze_specs(
    goal: str,
    topic: CollabTopic,
    sess: dict[str, Any],
    *,
    default_action_kind: str,
    default_job: str,
    human_feedback: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    from mino_nexus.core.execution_user import execution_user
    from mino_nexus.services.qa_cover import run_llm_job

    requirement = {
        "id": topic.topic_id,
        "title": topic.title or "议题",
        "source_text": goal,
        "human_feedback": str(human_feedback or "").strip(),
    }
    with execution_user(_uid(sess)):
        art = run_llm_job("analyze_req", requirement=requirement, cases=[], qa_process=None)
    if art.get("status") != "ok":
        return _line_specs(goal, default_action_kind, default_job), art
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    specs: list[dict[str, Any]] = []
    ac_rows = payload.get("ac") or []
    if isinstance(ac_rows, list):
        for i, row in enumerate(ac_rows[:12]):
            if isinstance(row, dict):
                title = str(row.get("title") or row.get("id") or f"验收点 {i + 1}").strip()
                detail = str(row.get("desc") or row.get("criteria") or row.get("text") or title).strip()
            else:
                title = str(row).strip()
                detail = title
            if not title:
                continue
            specs.append(
                {
                    "title": title[:200],
                    "goal": detail,
                    "acceptance": detail,
                    "action_kind": "none",
                }
            )
    if not specs:
        summary = str(payload.get("summary") or "").strip()
        if summary:
            specs.append(
                {
                    "title": "需求分析",
                    "goal": summary,
                    "action_kind": default_action_kind,
                    "action_payload": {"job": default_job},
                    "_analyze_done": True,
                }
            )
        else:
            specs = _line_specs(goal, default_action_kind, default_job)
    else:
        specs.insert(
            0,
            {
                "title": "需求分析",
                "goal": str(payload.get("summary") or goal).strip(),
                "action_kind": default_action_kind,
                "action_payload": {"job": default_job},
                "_analyze_done": True,
            },
        )
    return specs, art


def decompose_topic(
    project_id: str,
    topic_id: str,
    sess: dict[str, Any],
    *,
    goal: str = "",
    input_version: str = "",
    items: list[dict[str, Any]] | None = None,
    default_action_kind: str = "llm_job",
    default_job: str = "analyze_req",
    use_llm: bool = True,
    bump_version: bool = False,
    include_case_run: bool = True,
    idempotency_suffix: str = "",
) -> dict[str, Any]:
    _require_access(project_id, sess)
    raise ValueError('此协作功能已退出测试工作区主线')










def run_topic_pipeline(
    project_id: str,
    topic_id: str,
    sess: dict[str, Any],
    *,
    goal: str = "",
    bump_version: bool = True,
    engine: str = "langgraph",
) -> dict[str, Any]:
    _require_access(project_id, sess)
    raise ValueError('此协作功能已退出测试工作区主线')










def _work_item_next_steps(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    for row in items:
        if str(row.get("status") or "") in ("done", "running"):
            continue
        kind = str(row.get("action_kind") or "none")
        wid = str(row.get("work_item_id") or "")
        title = str(row.get("title") or "工作项")
        if kind == "apply_drafts":
            steps.append({"work_item_id": wid, "action": "apply_drafts", "label": f"确认写入用例草稿：{title}"})
        elif kind == "case_run":
            steps.append({"work_item_id": wid, "action": "case_run", "label": f"触发用例执行：{title}"})
        elif kind == "v5_publish":
            steps.append({"work_item_id": wid, "action": "v5_publish", "label": f"正式发布用例：{title}"})
        elif kind == "qa_tick":
            steps.append({"work_item_id": wid, "action": "qa_tick", "label": f"推进 QA：{title}"})
        elif kind == "llm_job":
            steps.append({"work_item_id": wid, "action": "llm_job", "label": f"运行分析：{title}"})
        elif kind == "openhands":
            steps.append({"work_item_id": wid, "action": "openhands", "label": f"提交 OpenHands：{title}"})
        elif str(row.get("status") or "") == "needs_correction":
            steps.append({"work_item_id": wid, "action": "review", "label": f"待纠正：{title}"})
    return steps[:8]


def advance_topic(
    project_id: str,
    topic_id: str,
    sess: dict[str, Any],
    *,
    goal: str = "",
    message: str = "",
    sync_memory: bool = False,
    engine: str = "langgraph",
) -> dict[str, Any]:
    """一键推进：测试交付流水线（需求→用例工作项），可选发消息、记忆。"""
    text = str(message or "").strip()
    orch: dict[str, Any] = {}
    if text:
        posted = post_message(project_id, topic_id, sess, body=text, auto_orchestrate=True)
        orch = posted.get("orchestration") if isinstance(posted.get("orchestration"), dict) else {}
        if str(orch.get("intent") or "") in ("run_case", "advance_pending"):
            items = list_work_items(project_id, topic_id, sess)
            next_steps = _work_item_next_steps(items)
            hint = orch.get("replies", [""])[0] if orch.get("replies") else (next_steps[0]["label"] if next_steps else "已按讨论执行")
            return {
                "items": items,
                "pipeline": {"skipped": True, "reason": orch.get("intent")},
                "next_steps": next_steps,
                "hint": str(hint)[:500],
                "orchestration": orch,
            }
    use_goal = str(goal or "").strip()
    if use_goal:
        update_topic(project_id, topic_id, sess, goal=use_goal)
    if sync_memory:
        try:
            refresh_topic_memory(project_id, topic_id, sess)
        except (KeyError, ValueError):
            pass
    outcome = run_topic_pipeline(
        project_id,
        topic_id,
        sess,
        goal=use_goal,
        bump_version=False,
        engine=engine,
    )
    items = list(outcome.get("items") or [])
    if not items:
        items = list_work_items(project_id, topic_id, sess)
    next_steps = _work_item_next_steps(items)
    blocked = bool((outcome.get("pipeline") or {}).get("blocked"))
    hint = (
        str((outcome.get("pipeline") or {}).get("block_reason") or "流水线有待纠正项，请查看工作项。")
        if blocked
        else (next_steps[0]["label"] if next_steps else "暂无待办工作项，可补充讨论后再次推进。")
    )
    outcome["next_steps"] = next_steps
    outcome["hint"] = hint
    return outcome


def refresh_topic_memory(project_id: str, topic_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    _require_access(project_id, sess)
    raise ValueError('此协作功能已退出测试工作区主线')










def bind_topic_im(
    project_id: str,
    topic_id: str,
    sess: dict[str, Any],
    *,
    channel: str,
    chat_id: str,
    node_id: str = "",
    label: str = "",
) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic

    _require_access(project_id, sess)
    ch = str(channel or "").strip()
    cid = str(chat_id or "").strip()
    if not ch or not cid:
        raise ValueError("missing_im_route")
    now = _now()
    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        topic.im_route = {
            "channel": ch,
            "chat_id": cid,
            "node_id": str(node_id or "").strip(),
            "label": str(label or "").strip(),
            "bound_at": now,
        }
        topic.updated_at = now
        db.flush()
        return _topic_public(topic, _load_topic_members(db, topic_id))


def mirror_im_to_collab(inbound: dict[str, Any]) -> None:
    from sqlalchemy import text

    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabMessage, CollabTopic

    channel = str(inbound.get("channel") or "").strip()
    chat_id = str(inbound.get("chat_id") or "").strip()
    body = str(inbound.get("text") or "").strip()
    sender = str(inbound.get("sender_id") or inbound.get("sender") or "im")
    if not channel or not chat_id or not body:
        return
    now = _now()
    with session_scope() as db:
        ids = [
            r[0]
            for r in db.execute(
                text(
                    "SELECT topic_id FROM collab_topics "
                    "WHERE json_extract(im_route, '$.chat_id') = :cid "
                    "AND json_extract(im_route, '$.channel') = :ch"
                ),
                {"cid": chat_id, "ch": channel},
            ).fetchall()
        ]
        if not ids:
            return
        rows = db.query(CollabTopic).filter(CollabTopic.topic_id.in_(ids)).all()
        for topic in rows:
            db.add(
                CollabMessage(
                    message_id=str(uuid.uuid4()),
                    topic_id=topic.topic_id,
                    author_kind="im",
                    author_id=sender[:120],
                    acting_bot_id="",
                    body=body[:4000],
                    created_at=now,
                )
            )
            topic.updated_at = now
        db.flush()


def apply_work_item_drafts(
    project_id: str,
    work_item_id: str,
    sess: dict[str, Any],
    *,
    confirm: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    _require_access(project_id, sess)
    raise ValueError('此协作功能已退出测试工作区主线')










def transfer_work_item(
    project_id: str,
    work_item_id: str,
    sess: dict[str, Any],
    *,
    holder_user_id: str = "",
    assignee_kind: str = "",
    assignee_id: str = "",
) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem

    _require_access(project_id, sess)
    now = _now()
    with session_scope() as db:
        wi = db.get(CollabWorkItem, work_item_id)
        if wi is None:
            raise KeyError("work_item_not_found")
        topic = db.get(CollabTopic, wi.topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        if str(wi.status or "") == "running":
            raise ValueError("work_item_running")
        if holder_user_id:
            wi.holder_user_id = str(holder_user_id).strip()
        if assignee_kind:
            wi.assignee_kind = str(assignee_kind).strip()
        if assignee_id:
            wi.assignee_id = str(assignee_id).strip()
        wi.updated_at = now
        db.flush()
        return _work_item_public(wi)


def trigger_work_item(
    project_id: str,
    work_item_id: str,
    sess: dict[str, Any],
    *,
    force_retry: bool = False,
    **_legacy: Any,
) -> dict[str, Any]:
    """立即执行：重置为 pending 并由 collab worker 消化（无独立执行路径）。"""
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem

    _require_access(project_id, sess)
    uid = _uid(sess)
    now = _now()
    with session_scope() as db:
        wi = db.get(CollabWorkItem, work_item_id)
        if wi is None:
            raise KeyError("work_item_not_found")
        topic = db.get(CollabTopic, wi.topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        holder = str(wi.holder_user_id or "").strip()
        if holder and holder != uid:
            raise PermissionError("execution_owner_mismatch")
        if str(wi.status or "") in ("running", "waiting"):
            raise ValueError("work_item_running")
        if force_retry:
            wi.attempt = int(wi.attempt or 0) + 1
            wi.run_id = ""
            wi.evidence = {}
        wi.status = "pending"
        wi.blocked_reason = ""
        wi.claimed_by = ""
        wi.claimed_at = ""
        wi.updated_at = now
        db.flush()
        pub = _work_item_public(wi)
    try:
        from mino_nexus.services.collab_worker import wake

        wake()
    except Exception:
        pass
    return {"work_item": pub, "queued": True}


def redo_work_item(
    project_id: str,
    work_item_id: str,
    sess: dict[str, Any],
    *,
    feedback: str = "",
) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem

    _require_access(project_id, sess)
    now = _now()
    with session_scope() as db:
        wi = db.get(CollabWorkItem, work_item_id)
        if wi is None:
            raise KeyError("work_item_not_found")
        topic = db.get(CollabTopic, wi.topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        payload = dict(wi.action_payload or {})
        if feedback.strip():
            payload["human_feedback"] = feedback.strip()
            wi.action_payload = payload
        wi.attempt = int(wi.attempt or 0) + 1
        wi.status = "pending"
        wi.blocked_reason = ""
        wi.run_id = ""
        wi.updated_at = now
        db.flush()
        pub = _work_item_public(wi)
    from mino_nexus.services.collab_worker import wake

    wake()
    return {"work_item": pub}


def answer_work_item(
    project_id: str,
    work_item_id: str,
    sess: dict[str, Any],
    *,
    fact: str,
) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic, CollabWorkItem

    _require_access(project_id, sess)
    text = str(fact or "").strip()
    if not text:
        raise ValueError("empty_fact")
    now = _now()
    with session_scope() as db:
        wi = db.get(CollabWorkItem, work_item_id)
        if wi is None:
            raise KeyError("work_item_not_found")
        topic = db.get(CollabTopic, wi.topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        if str(wi.status or "") != "blocked":
            raise ValueError("not_blocked")
        payload = dict(wi.action_payload or {})
        payload["answered_fact"] = text
        wi.action_payload = payload
        wi.status = "pending"
        wi.blocked_reason = ""
        wi.updated_at = now
        db.flush()
        pub = _work_item_public(wi)
    from mino_nexus.services.collab_worker import wake

    wake()
    return {"work_item": pub}


def pause_topic(project_id: str, topic_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic

    _require_access(project_id, sess)
    with session_scope() as db:
        row = db.get(CollabTopic, topic_id)
        if row is None or row.project_id != project_id:
            raise KeyError("topic_not_found")
        row.paused = 1
        row.updated_at = _now()
        db.flush()
        return _topic_public(row, _load_topic_members(db, topic_id))


def resume_topic(project_id: str, topic_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.mino_bots import CollabTopic

    _require_access(project_id, sess)
    with session_scope() as db:
        row = db.get(CollabTopic, topic_id)
        if row is None or row.project_id != project_id:
            raise KeyError("topic_not_found")
        row.paused = 0
        row.updated_at = _now()
        db.flush()
        out = _topic_public(row, _load_topic_members(db, topic_id))
    from mino_nexus.services.collab_worker import wake

    wake()
    return out


def close_topic(project_id: str, topic_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    return update_topic(project_id, topic_id, sess, status="closed")


def delete_topic(project_id: str, topic_id: str, sess: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """删除议题及其消息、成员、工作项、审批和协作任务行。

    不删除用例、运行记录和 session 证据：这些属于项目，不属于议题。
    有工作项正在被 worker 执行时拒绝，除非 force；已提交的跑批不会被取消。
    """
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.collab_approval import CollabApproval
    from mino_nexus.models.mino_bots import CollabActivity, CollabMessage, CollabTopicMember
    from mino_nexus.models.task import Task
    from mino_nexus.models.collab_message_job import CollabMessageJob

    _require_access(project_id, sess)
    with session_scope() as db:
        topic = db.get(CollabTopic, topic_id)
        if topic is None or topic.project_id != project_id:
            raise KeyError("topic_not_found")
        if db.query(CollabMessageJob).filter_by(topic_id=topic_id, status="running").first():
            raise ValueError("topic_has_running_message")
        items = db.query(CollabWorkItem).filter_by(topic_id=topic_id).all()
        if not force and any(str(w.status or "") == "running" for w in items):
            raise ValueError("topic_has_running_work")
        task_ids = [w.task_id for w in items if w.task_id]
        tid = topic.topic_id
        db.query(CollabApproval).filter_by(topic_id=tid).delete(synchronize_session=False)
        db.query(CollabActivity).filter_by(topic_id=tid).delete(synchronize_session=False)
        db.query(CollabWorkItem).filter_by(topic_id=tid).delete(synchronize_session=False)
        db.query(CollabMessageJob).filter_by(topic_id=tid).delete(synchronize_session=False)
        db.query(CollabMessage).filter_by(topic_id=tid).delete(synchronize_session=False)
        db.query(CollabTopicMember).filter_by(topic_id=tid).delete(synchronize_session=False)
        if task_ids:
            db.query(Task).filter(Task.id.in_(task_ids), Task.task_type == "collab_work_item").delete(synchronize_session=False)
        deleted = (
            db.query(CollabTopic)
            .filter(CollabTopic.topic_id == tid, CollabTopic.project_id == project_id)
            .delete(synchronize_session=False)
        )
        if deleted != 1:
            raise KeyError("topic_not_found")
        db.flush()
    return {"deleted": True, "topic_id": topic_id, "work_items": len(items)}


def get_topic_digest(project_id: str, topic_id: str, sess: dict[str, Any]) -> dict[str, Any]:
    topic = get_topic(project_id, topic_id, sess)
    return dict(topic.get("digest") or {})


def _trigger_llm_job(topic: CollabTopic, wi: CollabWorkItem, payload: dict[str, Any], sess: dict[str, Any]) -> dict[str, Any]:
    job = str(payload.get("job") or "analyze_req").strip()
    from mino_nexus.services.qa_cover import run_llm_job

    requirement = {
        "id": topic.topic_id,
        "title": wi.title or topic.title,
        "source_text": wi.goal or topic.goal,
        "human_feedback": "",
    }
    if payload.get("requirement_id"):
        requirement["id"] = str(payload["requirement_id"])
    art = run_llm_job(job, requirement=requirement, cases=[], qa_process=None)
    if art.get("status") != "ok":
        return {
            "status": "blocked",
            "reason_code": "llm_job_failed",
            "message": str(art.get("error") or "模型任务失败"),
            "artifact": art,
        }
    return {
        "status": "ok",
        "reason_code": "llm_candidate_only",
        "message": "已生成候选结果，未自动写入需求或用例",
        "artifact": art,
    }


def _trigger_qa_tick(
    project_id: str,
    topic: CollabTopic,
    wi: CollabWorkItem,
    payload: dict[str, Any],
    sess: dict[str, Any],
) -> dict[str, Any]:
    from mino_nexus.core.execution_user import user_thread
    from mino_nexus.services import app_automation as aas
    from mino_nexus.services import project_store as ps
    from mino_nexus.services import qa_process_jobs as cover_jobs
    from mino_nexus.services.qa_cover import tick

    app_id = str(payload.get("app_id") or topic.app_id or "").strip()
    if not app_id:
        return {"status": "blocked", "reason_code": "missing_app", "message": "议题未绑定 app_id"}
    try:
        app = ps.require_app(app_id)
    except KeyError:
        return {"status": "blocked", "reason_code": "app_not_found", "message": "应用不存在"}
    if str(app.get("project_id") or "") != project_id:
        return {"status": "blocked", "reason_code": "app_scope", "message": "应用不属于当前项目"}
    cfg = aas.get_automation_config(app)
    cases = aas.list_app_cases(app)
    note = str(wi.goal or topic.goal or "").strip()
    try:
        job = cover_jobs.create(app_id=app_id, requirement_id=str(payload.get("requirement_id") or topic.topic_id), jobs=list(payload.get("jobs") or []))
    except cover_jobs.JobConflict as exc:
        return {
            "status": "blocked",
            "reason_code": "qa_job_running",
            "message": "已有 QA 推进任务在跑",
            "job": exc.job.public(),
        }

    def flush(doc: dict) -> None:
        row = ps.find_app(app_id)
        if row:
            aas.save_automation_config(row, {"qa_process": doc})

    job.flush = flush
    job.report(phase="queued", label="已排队")
    seed = dict(cfg.get("qa_process") or {})
    job.save(seed)
    holder = str(wi.holder_user_id or _uid(sess)).strip()
    jid = job.id

    def _run() -> None:
        from mino_nexus.core.execution_user import execution_user

        row = cover_jobs.get(jid)
        if not row:
            return
        tok = cover_jobs.bind(row)
        try:
            with execution_user(holder):
                row.report(phase="running", label="协作议题触发 QA tick")
                fresh = aas.get_automation_config(ps.require_app(app_id))
                qa_doc = dict(fresh.get("qa_process") or seed)
                out = tick(
                    qa_process=qa_doc,
                    cases=cases,
                    project_id=project_id,
                    requirement_id=str(payload.get("requirement_id") or topic.topic_id),
                    user_note=note,
                    force=bool(payload.get("force")),
                    jobs=list(payload.get("jobs") or []),
                    replace_cases=bool(payload.get("replace_cases")),
                )
                row.finish(out)
        except cover_jobs.Cancelled:
            row.status = "cancelled"
        except Exception as exc:
            row.fail(str(exc))
        finally:
            cover_jobs.reset(tok)

    user_thread(target=_run, daemon=True, name=f"collab-qa-tick-{jid}").start()
    return {
        "status": "ok",
        "reason_code": "qa_tick_started",
        "message": "已排队 QA 流程推进",
        "qa_job_id": jid,
        "poll_path": f"/app-automation/qa-process/job/{jid}",
    }


def _trigger_openhands(wi: CollabWorkItem, payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    from mino_nexus.services.collab_openhands import openhands_status, resolve_openhands_api_url

    base, _src = resolve_openhands_api_url()
    base = str(base or payload.get("api_url") or "").strip().rstrip("/")
    oh = openhands_status()
    if not base:
        return {
            "status": "blocked",
            "reason_code": "openhands_not_configured",
            "message": oh.get("message") or "请配置 OPENHANDS_API_URL",
            "openhands": oh,
            "docs": "https://github.com/All-Hands-AI/OpenHands",
        }
    prompt = str(payload.get("prompt") or wi.goal or wi.title or "").strip()
    if not prompt:
        return {"status": "blocked", "reason_code": "missing_prompt", "message": "缺少开发任务描述"}
    try:
        resp = httpx.post(
            f"{base}/api/conversations",
            json={"initial_user_msg": prompt},
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        return {"status": "error", "reason_code": "openhands_unreachable", "message": str(exc)}
    if resp.status_code >= 400:
        return {
            "status": "error",
            "reason_code": "openhands_http_error",
            "message": resp.text[:500],
            "status_code": resp.status_code,
        }
    body = resp.json() if resp.content else {}
    from mino_nexus.services.collab_openhands import openhands_conversation_urls

    urls = openhands_conversation_urls(base, body if isinstance(body, dict) else {})
    return {
        "status": "ok",
        "reason_code": "openhands_started",
        "message": "已提交 OpenHands 会话",
        "conversation": body,
        "openhands_url": base,
        "openhands_ui": urls,
        "openhands": openhands_status(),
    }


def _download_center_hint(blocked: list[dict[str, Any]]) -> dict[str, Any] | None:
    codes = set()
    for case in blocked:
        for item in case.get("blocked_by") or []:
            if isinstance(item, dict):
                codes.add(str(item.get("reason") or ""))
    tools: list[str] = []
    if "compile_artifact_unavailable" in codes or any("编译" in str(c.get("reasons") or []) for c in blocked):
        tools.append("browser.chromium")
    if not tools:
        return None
    return {
        "workbench_path": "/settings/runtime?pane=downloads",
        "tool_ids": tools,
        "message": "请在 Scout 节点下载中心补齐依赖后重试",
    }


def _trigger_case_run(
    project_id: str,
    topic: CollabTopic,
    wi: CollabWorkItem,
    payload: dict[str, Any],
    sess: dict[str, Any],
) -> dict[str, Any]:
    from mino_nexus.services import project_store as ps
    from mino_nexus.services.case_preflight import blocked_message, preflight

    app_id = str(payload.get("app_id") or topic.app_id or "").strip()
    if not app_id:
        return {
            "status": "blocked",
            "reason_code": "missing_app",
            "message": "缺少 app_id，无法派发执行",
        }
    try:
        app = ps.require_app(app_id)
    except KeyError:
        return {"status": "blocked", "reason_code": "app_not_found", "message": "应用不存在"}
    if str(app.get("project_id") or "") != project_id:
        return {"status": "blocked", "reason_code": "app_scope", "message": "应用不属于当前项目"}

    case_ids = [str(x).strip() for x in (payload.get("case_ids") or []) if str(x).strip()]
    if not case_ids:
        return {
            "status": "blocked",
            "reason_code": "missing_case_ids",
            "message": "缺少 case_ids",
        }
    try:
        summary = preflight(app, case_ids)
    except ValueError as exc:
        return {"status": "blocked", "reason_code": "preflight_rejected", "message": str(exc)}
    blocked = list(summary.get("blocked") or [])
    if blocked:
        hint = _download_center_hint(blocked)
        body: dict[str, Any] = {
            "status": "blocked",
            "reason_code": "compile_or_binding_blocked",
            "message": blocked_message(blocked),
            "preflight": summary,
        }
        if hint:
            body["download_center"] = hint
        return body

    from mino_nexus.loop import case_runner as cr

    holder = str(wi.holder_user_id or _uid(sess)).strip()
    out = cr.run_cases(
        app,
        sn=str(payload.get("sn") or ""),
        case_ids=case_ids,
        async_exec=bool(payload.get("async_exec", True)),
        platform=str(payload.get("platform") or "android"),
        plugin_user_id=holder,
        run_type="collab",
    )
    return {
        "status": "ok",
        "reason_code": "case_run_started",
        "message": "已提交执行",
        "run": out,
    }
