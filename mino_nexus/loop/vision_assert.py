"""P6-P3：agent-vision-assert（批量校验，check 阶段）。"""
from __future__ import annotations

import json
from typing import Any, Optional

from mino_nexus.ai.planner import assert_visual
from mino_nexus.ai.schemas import AssertResult
from mino_nexus.loop.milestones import read_state
from mino_nexus.loop.vision_flags import vision_assert_v1_enabled


def _render_assert_job(slots: dict[str, str]) -> tuple[Any, Any]:
    from mino_nexus.ai.prompt_render import JobRenderError, render

    for jid in ("agent-vision-assert", "assert-vision"):
        try:
            return render(jid, slots)
        except JobRenderError:
            continue
    raise JobRenderError("agent-vision-assert / assert-vision 均未配置")


def vision_assert_checkpoints(
    *,
    expectation: str,
    image_base64: str,
    image_mime: str = "image/png",
    context_block: str = "",
    provider_id: str | None = None,
    checkpoints: list[dict[str, Any]] | None = None,
) -> AssertResult:
    """批量校验点；优先 job agent-vision-assert。"""
    from mino_nexus.ai.job_slots import assemble_assert_vision_slots
    from mino_nexus.ai.planner import _llm_image, _parse_assert_result, _chat
    from mino_nexus.ai.llm_client import resolve_regression_provider

    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        return AssertResult(
            passed=False,
            confidence=0.0,
            ai_reasoning=f"未启用 AI 视觉：{gate.get('reason')}",
            evidence="",
            parse_warnings=["provider unavailable"],
        )
    image_base64, image_mime = _llm_image(image_base64, image_mime)
    cp_block = ""
    if checkpoints:
        cp_block = "校验点 JSON：\n" + json.dumps(checkpoints, ensure_ascii=False, indent=2)
    merged_ctx = "\n\n".join(x for x in (context_block, cp_block) if x).strip()
    slots = assemble_assert_vision_slots(
        expectation=expectation,
        image_base64=image_base64,
        image_mime=image_mime,
        context_block=merged_ctx,
    )
    try:
        messages, job_meta = _render_assert_job(slots)
    except Exception as exc:  # noqa: BLE001
        return AssertResult(
            passed=False,
            confidence=0.0,
            ai_reasoning=str(exc),
            evidence="",
            parse_warnings=["job render failed"],
        )
    call = job_meta.get("call") or {}
    job_id = str(job_meta.get("id") or job_meta.get("job_id") or "agent-vision-assert")
    raw, meta = _chat(
        job=job_id if job_id in ("agent-vision-assert", "assert-vision") else "agent-vision-assert",
        provider=provider,
        messages=messages,
        job_meta=job_meta,
        temperature=float(call.get("temperature", 0.0)),
        max_tokens=int(call.get("max_tokens", 800)),
        timeout_sec=int(call.get("timeout_sec", 60)),
        json_mode=bool(call.get("json_mode", True)),
    )
    if raw is None:
        return AssertResult(
            passed=False,
            confidence=0.0,
            ai_reasoning="LLM 返回空",
            evidence="",
            parse_warnings=[str(meta.get("error") or "")[:160]],
            raw_llm={"meta": meta},
        )
    return _parse_assert_result(raw)


def checkpoints_from_milestones(cursor: Any) -> list[dict[str, Any]]:
    state = read_state(cursor)
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    out: list[dict[str, Any]] = []
    for row in ms:
        if not isinstance(row, dict):
            continue
        if str(row.get("kind") or "") != "checkpoint":
            continue
        assert_payload = row.get("assert") if isinstance(row.get("assert"), dict) else {}
        element = assert_payload.get("element") if isinstance(assert_payload.get("element"), dict) else {}
        rules = assert_payload.get("rules") if isinstance(assert_payload.get("rules"), dict) else {}
        item: dict[str, Any] = {
            "point_id": str(row.get("id") or ""),
            "title": str(row.get("title") or ""),
            "key_ref": str(row.get("key_ref") or ""),
            "checkpoint_kind": str(assert_payload.get("mode") or row.get("checkpoint_kind") or "element_exists"),
        }
        if element:
            item["element"] = element
        if rules:
            item["rules"] = rules
        if isinstance(assert_payload.get("bind"), dict):
            item["bind"] = assert_payload["bind"]
        if not element and not rules:
            item["expectation"] = str(assert_payload.get("expectation") or row.get("title") or "")
        out.append(item)
    from mino_nexus.loop.structured_check import hydrate_bound_text

    return hydrate_bound_text(cursor, out)


def run_vision_assert_if_enabled(
    *,
    cursor: Any,
    writer: Any,
    shot: Any,
    expected: str,
    context_block: str = "",
    provider_id: str = "",
) -> Optional[AssertResult]:
    if not vision_assert_v1_enabled():
        return None
    if getattr(cursor, "vision_assert_done", False):
        return None
    cps = checkpoints_from_milestones(cursor)
    structured = bool(cps) and all(isinstance(c, dict) and (c.get("element") or c.get("rules")) for c in cps)
    ctx_for_job = (
        "只按校验点 JSON 的 element 与 rules 判断。不要根据编号原文另找目标。"
        if structured
        else context_block
    )
    img = ""
    if callable(getattr(shot, "has_image", None)) and shot.has_image():
        img = str(getattr(shot, "image_base64", "") or "")
    result = vision_assert_checkpoints(
        expectation="" if structured else str(expected or ""),
        image_base64=img,
        image_mime=str(getattr(shot, "image_mime", "") or "image/png"),
        context_block=ctx_for_job,
        provider_id=provider_id or None,
        checkpoints=cps,
    )
    setattr(cursor, "vision_assert_done", True)
    if writer:
        writer.append(
            "assert/vision",
            {
                "passed": bool(result.passed),
                "confidence": float(result.confidence or 0),
                "checkpoints": len(cps),
                "evidence": str(result.evidence or "")[:300],
            },
        )
    return result
