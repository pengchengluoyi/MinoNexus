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


def _check_visible_scope(row: dict[str, Any], checkpoint: dict[str, Any]) -> bool:
    input_value = str(checkpoint.get("checkpoint_kind") or "") == "input_value"
    scope = str(row.get("evidence_scope") or ("" if input_value else "visible_full"))
    if scope == "visible_full":
        return not input_value or str(row.get("target_ref") or "") == str(checkpoint.get("point_id") or "")
    if scope != "visible_partial" or str(checkpoint.get("verification_policy") or "") != "visible_content":
        return False
    if row.get("hidden_content_verified") is not False:
        return False
    if str(row.get("target_ref") or "") != str(checkpoint.get("point_id") or ""):
        return False
    truncation = str(row.get("truncation") or "")
    match = str(row.get("match_kind") or "")
    modes = {
        "tail_ellipsis": "prefix", "tail_clipped": "prefix",
        "head_clipped": "suffix", "head_scrolled": "suffix",
    }
    if modes.get(truncation) != match:
        return False
    if truncation == "tail_ellipsis" and str(row.get("ellipsis_source") or "") != "rendered":
        return False
    rules = checkpoint.get("rules") if isinstance(checkpoint.get("rules"), dict) else {}
    expected = str(rules.get("expected_value") or rules.get("bound_text") or "")
    visible = str(row.get("visible_text") or "")
    # 通用短片段无法区分目标值；不以任意短子串放行。
    if not expected or len(visible.strip()) < 4 or visible == expected:
        return False
    if "@" in expected and match == "suffix":
        # 只露出 @gmail.com 等公共域名不足以区别本次输入的账号。
        if "@" not in visible or not visible.split("@", 1)[0].strip():
            return False
    candidates = [rules.get("previous_value"), *(rules.get("known_candidates") or [])]
    if any(
        isinstance(value, str) and (
            value.startswith(visible) if match == "prefix" else value.endswith(visible)
        ) for value in candidates if value
    ):
        return False
    return expected.startswith(visible) if match == "prefix" else expected.endswith(visible)


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
            error_kind="provider_unavailable",
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
            error_kind="assert_render_error",
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
            error_kind="assert_service_error",
        )
    if not isinstance(raw, dict):
        return AssertResult(
            passed=False, confidence=0.0, ai_reasoning="校验响应不是 JSON 对象",
            parse_warnings=["assert response must be an object"], error_kind="assert_output_invalid",
        )
    result = _parse_assert_result(raw)
    if result.parse_warnings:
        result.passed = False
        result.error_kind = "assert_output_invalid"
    if checkpoints:
        result.raw_llm = {key: value for key, value in raw.items() if key != "checkpoint_results"}
        requested = {str(row.get("point_id") or ""): row for row in checkpoints}
        rows = raw.get("checkpoint_results")
        seen: set[str] = set()
        valid: list[dict[str, Any]] = []
        errors: list[str] = []
        if "" in requested or len(requested) != len(checkpoints):
            errors.append("请求中的校验点 ID 为空或重复")
        if not isinstance(rows, list):
            errors.append("缺少逐项 checkpoint_results")
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                errors.append("逐项结果格式错误")
                continue
            point_id = str(row.get("point_id") or "")
            if point_id not in requested or point_id in seen:
                errors.append(f"未知或重复的校验点: {point_id}")
                continue
            seen.add(point_id)
            if type(row.get("passed")) is not bool or not str(row.get("evidence") or "").strip():
                errors.append(f"校验点结果或证据无效: {point_id}")
                continue
            if row["passed"] and not _check_visible_scope(row, requested[point_id]):
                errors.append(f"校验点验证范围不足: {point_id}")
                continue
            valid.append({
                "point_id": point_id,
                "passed": row["passed"],
                "evidence": str(row["evidence"]),
                "evidence_scope": str(row.get("evidence_scope") or (
                    "unknown" if str(requested[point_id].get("checkpoint_kind") or "") == "input_value"
                    else "visible_full"
                )),
                "match_kind": str(row.get("match_kind") or ""),
                "hidden_content_verified": row.get("hidden_content_verified") is True,
            })
        if seen != set(requested):
            errors.append("逐项结果未覆盖全部校验点")
        result.checkpoint_results = valid
        if errors:
            result.passed = False
            result.parse_warnings.extend(errors)
            result.error_kind = "assert_output_invalid"
        elif not result.error_kind:
            result.passed = all(row["passed"] for row in valid if requested[row["point_id"]].get("required") is not False)
    return result


def checkpoints_from_milestones(cursor: Any) -> list[dict[str, Any]]:
    state = read_state(cursor)
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    out: list[dict[str, Any]] = []
    for row in ms:
        if not isinstance(row, dict):
            continue
        if str(row.get("kind") or "") != "checkpoint":
            continue
        if str(row.get("status") or "pending").lower() in ("pass", "skipped"):
            continue
        assert_payload = row.get("assert") if isinstance(row.get("assert"), dict) else {}
        element = assert_payload.get("element") if isinstance(assert_payload.get("element"), dict) else {}
        rules = assert_payload.get("rules") if isinstance(assert_payload.get("rules"), dict) else {}
        item: dict[str, Any] = {
            "point_id": str(row.get("id") or ""),
            "required": not bool(row.get("optional")),
            "title": str(row.get("title") or ""),
            "key_ref": str(row.get("key_ref") or ""),
            "checkpoint_kind": str(assert_payload.get("mode") or row.get("checkpoint_kind") or "element_exists"),
        }
        item["verification_policy"] = str(rules.get("verification_policy") or (
            "visible_content" if item["checkpoint_kind"] == "input_value" else "exact_value"
        ))
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
    ctx_for_job = context_block
    img = ""
    if callable(getattr(shot, "has_image", None)) and shot.has_image():
        img = str(getattr(shot, "image_base64", "") or "")
    if not img:
        setattr(cursor, "vision_assert_done", True)
        if writer:
            writer.append("assert/vision", {
                "passed": False,
                "checkpoints": len(cps),
                "reason_code": "observation_missing",
                "status": "blocked",
                "error_kind": "observation_missing",
            })
        return AssertResult(
            passed=False,
            confidence=0.0,
            ai_reasoning="本轮没有可用截图，不能进行看图验收",
            evidence="",
            parse_warnings=["observation missing"],
            error_kind="observation_missing",
        )
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
        import hashlib

        writer.append(
            "assert/vision",
            {
                "observation_turn": int(getattr(writer, "turn", 0) or 0),
                "raw_image_hash": hashlib.sha256(img.encode("ascii", errors="ignore")).hexdigest()[:20],
                "passed": bool(result.passed),
                "status": "blocked" if result.error_kind else ("pass" if result.passed else "fail"),
                "error_kind": result.error_kind,
                "confidence": float(result.confidence or 0),
                "checkpoints": len(cps),
                "evidence": str(result.evidence or "")[:300],
                "checkpoint_results": [
                    {
                        "point_id": str(row.get("point_id") or ""),
                        "passed": row.get("passed") is True,
                        "evidence_scope": str(row.get("evidence_scope") or "unknown"),
                        "hidden_content_verified": row.get("hidden_content_verified") is True,
                    }
                    for row in result.checkpoint_results
                ],
                "parse_warnings": list(result.parse_warnings)[:5],
            },
        )
    return result
