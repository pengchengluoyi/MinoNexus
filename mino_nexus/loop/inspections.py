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
        # 其它 observe job 后续按同一模式扩展
