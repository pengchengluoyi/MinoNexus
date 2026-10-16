"""只读截图核对显式目标是否为真实复选框及其状态；不从缺失推断不适用。"""
from __future__ import annotations

import hashlib
import json
import math


def validate_observation(raw):
    if not isinstance(raw, dict) or set(raw) != {"target_matches", "observable", "checked", "evidence"}:
        return False
    return (raw.get("target_matches") is True and raw.get("observable") is True
            and type(raw.get("checked")) is bool and isinstance(raw.get("evidence"), str)
            and bool(raw["evidence"].strip()))


def observe_checkbox(ctx, proxy, target, *, consent=False, login_scope=False):
    bounds = target.get("bounds") if isinstance(target, dict) else None
    snapshot = str((getattr(ctx, "observation_contract", None) or {}).get("snapshot_id") or "")
    unavailable = {"verified": False, "checked": None, "snapshot_id": snapshot}
    if (not snapshot or not isinstance(bounds, (list, tuple)) or len(bounds) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds)):
        return unavailable
    target_snapshot = target.get("snapshot_id")
    if target_snapshot and target_snapshot != snapshot:
        return unavailable
    try:
        shot = proxy.observe("screenshot", force_fresh=True)
    except Exception:
        return unavailable
    image = str(getattr(shot, "image_base64", "") or "")
    width, height = int(getattr(shot, "width", 0) or 0), int(getattr(shot, "height", 0) or 0)
    coordinate_wh = (getattr(ctx, "observation_contract", None) or {}).get("viewport_wh")
    if str(getattr(ctx, "platform", "")).lower() == "web":
        if (not isinstance(coordinate_wh, (list, tuple)) or len(coordinate_wh) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in coordinate_wh)):
            return unavailable
        cw, ch = coordinate_wh
        if width <= 0 or height <= 0 or abs(width / height - cw / ch) > 0.01:
            return unavailable
    else:
        cw, ch = width, height
    if (not image or width <= 0 or height <= 0 or not 0 <= bounds[0] < bounds[2] <= cw
            or not 0 <= bounds[1] < bounds[3] <= ch):
        return unavailable
    from mino_nexus.ai.llm_client import resolve_regression_provider
    provider, gate = resolve_regression_provider()
    if provider is None:
        return unavailable
    from mino_nexus.loop.observe.agent_stream import make_llm_jpeg
    prepared, mime = make_llm_jpeg(image, compress_ratio=1.0)
    if not prepared:
        return unavailable
    from mino_nexus.ai.prompt_render import render
    box = [round(float(v) * 1000 / (cw if i % 2 == 0 else ch)) for i, v in enumerate(bounds)]
    schema = {"title": "checkbox_observation", "type": "object", "properties": {
        "target_matches": {"type": "boolean"}, "observable": {"type": "boolean"},
        "checked": {"type": ["boolean", "null"]}, "evidence": {"type": "string"}},
        "required": ["target_matches", "observable", "checked", "evidence"], "additionalProperties": False}
    job = "inspect-login-consent-scope" if login_scope else "inspect-checkbox-state"
    if login_scope:
        schema = {"title": "login_consent_scope", "type": "object", "properties": {
            "login_form_visible": {"type": "boolean"}, "whole_form_visible": {"type": "boolean"},
            "consent_control_absent": {"type": "boolean"}, "evidence": {"type": "string"}},
            "required": ["login_form_visible", "whole_form_visible", "consent_control_absent", "evidence"],
            "additionalProperties": False}
    try:
        messages, job_meta = render(job, {
            "image_base64": prepared, "image_mime": mime,
            "target": json.dumps({"bounds_1000": box, "purpose": "完整登录表单范围" if login_scope else "登录协议同意框" if consent else "明确指定的复选框"}, ensure_ascii=False),
            "response_schema": json.dumps(schema, ensure_ascii=False)})
    except Exception:
        return unavailable
    from mino_nexus.ai.planner import _chat, _case_model_deadline, _clamp_to_case_deadline
    raw, meta = _chat(job=job, provider=provider, messages=messages, job_meta=job_meta,
                     temperature=0.0, max_tokens=450, timeout_sec=_clamp_to_case_deadline(ctx, 45),
                     deadline_monotonic=_case_model_deadline(ctx), json_mode=True, response_schema=schema)
    verified = validate_observation(raw)
    if login_scope:
        verified = (isinstance(raw, dict) and set(raw) == {"login_form_visible", "whole_form_visible", "consent_control_absent", "evidence"}
                    and all(raw.get(k) is True for k in ("login_form_visible", "whole_form_visible", "consent_control_absent"))
                    and isinstance(raw.get("evidence"), str) and bool(raw["evidence"].strip()))
    result = {"verified": verified, "checked": raw["checked"] if verified and not login_scope else None,
              "snapshot_id": snapshot, "image_hash": hashlib.sha256(image.encode()).hexdigest()[:20],
              "bounds": list(bounds), "viewport_wh": [width, height], "verified_by": job, "scope_absent": bool(login_scope and verified)}
    from mino_nexus.loop.observe.session_log import active_writer
    writer = active_writer()
    if writer:
        writer.append("checkbox/visual_observation", result)
    return result
