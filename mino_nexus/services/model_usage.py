"""模型用量账本与账号内聚合。未知成本与零成本严格区分。"""
from __future__ import annotations
from datetime import datetime, timezone
import uuid
from sqlalchemy import func


def begin_attempt(*, provider: dict, meta: dict, kind: str = "chat", model: str = "", attempt: int = 1) -> str:
    """先落 pending 再发请求；崩溃/记录失败仍有未知用量记录，不能漏成免费。"""
    from mino_nexus.core.execution_user import require_user_id
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.model_usage import ModelUsage
    from mino_nexus.ai.dispatch_log import ctx
    from mino_nexus.loop.observe.session_log import active_writer
    owner = require_user_id()
    if owner != provider.get("owner_user_id"):
        raise PermissionError("模型用量必须归属当前执行账号")
    env, writer = ctx(), active_writer()
    call_id = "mu-" + uuid.uuid4().hex
    step = env.get("case_step_index", env.get("step_index"))
    with session_scope() as db:
        db.add(ModelUsage(id=call_id, owner_user_id=owner, at=datetime.now(timezone.utc).isoformat(),
            task_id=str(getattr(writer, "run_id", "") or env.get("task_id") or ""),
            case_id=str(getattr(writer, "case_id", "") or env.get("case_id") or ""),
            session_id=str(getattr(writer, "session_id", "") or env.get("session_id") or ""),
            step_index=int(step) if step is not None else None, turn=getattr(writer, "turn", None),
            provider_id=str(provider.get("id") or ""), model=model or str(provider.get("model") or ""), kind=kind,
            job=str(env.get("job") or ""), status="pending", payload_json={"attempt": attempt, "usage_status": "unknown", "cost_status": "unknown"}))
    meta.setdefault("usage_attempt_ids", []).append(call_id)
    return call_id


def record_attempt(*, provider: dict, meta: dict, response: dict | None, kind: str = "chat", model: str = "", attempt: int = 1, call_id: str | None = None) -> str:
    from mino_nexus.core.execution_user import current_user_id
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.model_usage import ModelUsage
    from mino_nexus.ai.dispatch_log import ctx
    from mino_nexus.ai.model_sdk import cost_meta, sdk_provider
    from mino_nexus.loop.observe.session_log import active_writer
    owner = current_user_id()
    if not owner or owner != provider.get("owner_user_id"):
        raise ValueError("模型用量必须归属当前执行账号")
    env, writer = ctx(), active_writer()
    billing = cost_meta(response, provider, model=model or str(provider.get("model") or ""), kind=kind)
    usage = billing.get("usage") or {}
    def count(key, fallback=None):
        value = usage.get(key, fallback)
        return max(0, int(value)) if isinstance(value, (int, float)) else None
    prompt, completion = count("prompt_tokens", usage.get("input_tokens")), count("completion_tokens", 0 if kind == "embedding" and usage else usage.get("output_tokens"))
    total = count("total_tokens", prompt + completion if prompt is not None and completion is not None else None)
    call_id = call_id or "mu-" + uuid.uuid4().hex
    task_id = str(getattr(writer, "run_id", "") or env.get("task_id") or "")
    case_id = str(getattr(writer, "case_id", "") or env.get("case_id") or "")
    session_id = str(getattr(writer, "session_id", "") or env.get("session_id") or "")
    step = env.get("case_step_index", env.get("step_index"))
    safe = {k: meta.get(k) for k in ("sdk_provider", "http_status", "provider_error_code", "provider_error_param", "provider_request_id", "fail_kind", "retryable", "elapsed_ms", "prompt_version", "prompt_sha")}
    safe.update(billing, attempt=attempt, sdk_provider=sdk_provider(provider))
    safe["prompt_version"] = meta.get("prompt_version") or env.get("prompt_version")
    safe["prompt_sha"] = meta.get("prompt_sha") or env.get("prompt_sha")
    with session_scope() as db:
        pending = db.get(ModelUsage, call_id)
        if pending is not None and pending.owner_user_id != owner:
            raise PermissionError("模型账本归属不匹配")
        at = pending.at if pending is not None else datetime.now(timezone.utc).isoformat()
        db.merge(ModelUsage(id=call_id, owner_user_id=owner, at=at, task_id=task_id, case_id=case_id,
                         session_id=session_id, step_index=int(step) if step is not None else None, turn=getattr(writer, "turn", None),
                         provider_id=str(provider.get("id") or ""), model=model or str(provider.get("model") or ""), kind=kind,
                         job=str(env.get("job") or ("connection-validation" if meta.get("validation") else "")), status="error" if meta.get("error") else "done",
                         prompt_tokens=prompt, completion_tokens=completion, total_tokens=total, estimated_cost_usd=billing["estimated_cost_usd"], payload_json=safe))
    meta.update(billing)
    if call_id not in meta.setdefault("usage_attempt_ids", []):
        meta["usage_attempt_ids"].append(call_id)
    return call_id


def query_usage(*, task_id: str = "", case_id: str = "", session_id: str = "", step_index: int | None = None,
                group_by: str = "task", start: str = "", end: str = "", offset: int = 0, limit: int = 100) -> dict:
    from mino_nexus.core.execution_user import require_user_id
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.model_usage import ModelUsage as M
    owner = require_user_id()
    groups = {"user": [M.owner_user_id], "task": [M.task_id], "case": [M.task_id, M.case_id],
              "step": [M.task_id, M.case_id, M.session_id, M.step_index], "model": [M.provider_id, M.model]}
    if group_by not in groups:
        raise ValueError("group_by 须为 user/task/case/step/model")
    def utc(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("起止时间须带时区")
        return parsed.astimezone(timezone.utc).isoformat()
    if start and end and utc(start) >= utc(end):
        raise ValueError("开始时间须早于结束时间")
    fields = [func.count(M.id).label("attempts"), func.sum(M.prompt_tokens).label("prompt_tokens"),
              func.sum(M.completion_tokens).label("completion_tokens"), func.sum(M.total_tokens).label("total_tokens"),
              func.sum(M.estimated_cost_usd).label("known_estimated_cost_usd"),
              func.count(M.total_tokens).label("reported_usage_attempts"), func.count(M.estimated_cost_usd).label("priced_attempts")]
    with session_scope() as db:
        q = db.query(M).filter(M.owner_user_id == owner)
        for column, value in ((M.task_id, task_id), (M.case_id, case_id), (M.session_id, session_id)):
            if value:
                q = q.filter(column == value)
        if step_index is not None:
            q = q.filter(M.step_index == step_index)
        if start:
            q = q.filter(M.at >= utc(start))
        if end:
            q = q.filter(M.at < utc(end))
        def decorate(row):
            out = dict(row._mapping)
            out["unknown_usage_attempts"] = out["attempts"] - out["reported_usage_attempts"]
            out["unknown_cost_attempts"] = out["attempts"] - out["priced_attempts"]
            out["cost_complete"] = out["unknown_cost_attempts"] == 0
            return out
        total = decorate(q.with_entities(*fields).one())
        cols = groups[group_by]
        grouped = q.with_entities(*cols, *fields).group_by(*cols)
        group_count = grouped.count()
        rows = grouped.order_by(*cols).offset(offset).limit(limit).all()
        return {"currency": "USD", "cost_type": "estimated", "owner_user_id": owner, "group_by": group_by,
                "summary": total, "items": [decorate(row) for row in rows], "group_count": group_count,
                "offset": offset, "has_more": offset + len(rows) < group_count,
                "notice": "金额是调用时价格表估算，并非服务商账单；未知用量/价格单列，历史未记录的调用不自动补估。"}
