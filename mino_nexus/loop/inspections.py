"""阶段边界巡检：只产文本槽，不派设备动作。"""
from __future__ import annotations

from typing import Any

from mino_nexus.ai.planner import inspect_session


def format_session_block(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        reason = str(result.get("reason") or "未观察").strip()
        return f"（会话观察未成功：{reason}）"
    return (
        f"session={result.get('session')} identity={result.get('identity')} "
        f"seen={result.get('seen')} next={result.get('next')} "
        f"reason={result.get('reason')}"
    ).strip()


def run_inspections(
    specs: list[dict[str, Any]],
    *,
    at: str,
    shot,
    ctx,
    provider_id: str = "",
    slot_sink: dict[str, str],
    required_session: str = "",
) -> None:
    """在指定时机跑声明的巡检 job，结果写入 slot_sink。"""
    mark = str(at or "").strip()
    if not mark:
        return
    for spec in specs or []:
        if str(spec.get("at") or "") != mark:
            continue
        job_id = str(spec.get("job") or "").strip()
        if job_id == "inspect-session":
            if not shot or not getattr(shot, "has_image", lambda: False)():
                slot_sink["session_block"] = "（无截图，跳过会话观察）"
                continue
            row = inspect_session(
                required_session=required_session,
                knowledge_hint=str(slot_sink.get("knowledge_hint") or ""),
                accounts_brief=str(getattr(ctx, "accounts_brief", "") or ""),
                image_base64=str(getattr(shot, "image_base64", "") or ""),
                image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
                provider_id=provider_id or None,
            )
            slot_sink["session_block"] = format_session_block(row)
            _log_inspection(mark=mark, job_id=job_id, result=row, session_block=slot_sink["session_block"])
        # 其它 observe job 后续按同一模式扩展


def _log_inspection(
    *,
    mark: str,
    job_id: str,
    result: dict[str, Any] | None = None,
    session_block: str = "",
) -> None:
    try:
        from mino_nexus.loop.session_log import active_writer

        writer = active_writer()
        if writer is None:
            return
        row = dict(result or {})
        writer.append(
            "inspection/done",
            {
                "at": mark,
                "job_id": job_id,
                "ok": bool(row.get("ok")),
                "session_block": str(session_block or "")[:1200],
                "session": str(row.get("session") or ""),
                "identity": str(row.get("identity") or ""),
                "next": str(row.get("next") or ""),
                "reason": str(row.get("reason") or "")[:400],
            },
        )
    except Exception:
        pass
