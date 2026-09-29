"""逻辑块步级 / 块级准出。输入必须读回；块级失败拦住 do→check。"""
from __future__ import annotations

import re
from typing import Any

_FOCUS_BAD_RE = re.compile(
    r"焦点未确认|未确认焦点|focus\s*not\s*confirmed|focus\s*uncertain",
    re.I,
)
_SMS_FIELD_RE = re.compile(r"sms_code|验证码|\(sms_code\)|field\s*=\s*sms", re.I)
_INPUT_OK_RE = re.compile(r"输入|键入|填入|type[d]?", re.I)
_COORD_PARENS_RE = re.compile(r"\(\s*\d+\s*,\s*\d+\s*\)")
_COORD_AT_RE = re.compile(r"@\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)")


def _landing_px(summary: str) -> tuple[int, int] | None:
    """设备回执里的落点是视口像素，例如 输入(sms_code)@(1037,410)。"""
    m = _COORD_AT_RE.search(str(summary or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _sent_vs_landing(
    summary: str,
    sent_x: Any,
    sent_y: Any,
    viewport_w: int,
    viewport_h: int,
) -> tuple[bool | None, str]:
    """千分比 × 视口 对回执像素。无法比较时返回 (None, "")。"""
    if sent_x is None or sent_y is None or viewport_w <= 0 or viewport_h <= 0:
        return None, ""
    landing = _landing_px(summary)
    if landing is None:
        return None, ""
    from mino_nexus.ai.coords import landing_matches_sent, milli_to_viewport_px

    sent_px = milli_to_viewport_px(sent_x, sent_y, viewport_w, viewport_h)
    ok = landing_matches_sent(
        sent_x,
        sent_y,
        landing[0],
        landing[1],
        width=viewport_w,
        height=viewport_h,
    )
    detail = f"{sent_x},{sent_y}->{sent_px} vs ({landing[0]},{landing[1]})"
    return ok, detail


def _summary_without_coordinate_numbers(summ: str) -> str:
    s = _COORD_AT_RE.sub("", summ)
    s = _COORD_PARENS_RE.sub("", s)
    return s


def device_input_text_exit(
    *,
    summary: str,
    field: str = "",
    web_coordinate_only: bool = False,
    field_value: str | None = None,
    expected: str = "",
    sent_x: Any = None,
    sent_y: Any = None,
    viewport_w: int = 0,
    viewport_h: int = 0,
) -> tuple[str, str]:
    """返回 (exit_status, reason)。pass | fail | inconclusive。

    web_coordinate_only 只说明落点方式，不关闭焦点检查和读回。
    field_value 为 None 表示这次没有读回；空字符串表示读过但框里是空的。
    焦点是否落在派单点上：派单千分比按视口宽高换成像素，再和回执像素比。
    """
    del web_coordinate_only
    summ = str(summary or "").strip()
    fld = str(field or "").strip().lower()
    exp = str(expected or "").strip()
    landed, land_detail = _sent_vs_landing(
        summ, sent_x, sent_y, int(viewport_w or 0), int(viewport_h or 0)
    )
    if landed is False:
        return "fail", f"input_point_off_sent:{land_detail}"
    if field_value is not None and exp:
        got = str(field_value or "").strip()
        if not got:
            return "fail", "field_value_unread"
        if exp not in got and got not in exp:
            return "fail", "field_value_mismatch"
        return "pass", "field_value_readback"
    if _FOCUS_BAD_RE.search(summ):
        if landed is True:
            return "fail", f"focus_unconfirmed_at_sent_point:{land_detail}"
        return "fail", "device_summary_focus_unconfirmed"
    if fld in ("sms_code", "验证码", "otp"):
        if not summ:
            return "inconclusive", "empty_summary"
        if re.search(r"点击", summ):
            if re.search(r"@|\.com|email|邮箱|gmail", summ, re.I):
                return "fail", "sms_field_clicked_email"
            if not _INPUT_OK_RE.search(summ):
                return "fail", "sms_tap_without_input"
        body = _summary_without_coordinate_numbers(summ)
        if _INPUT_OK_RE.search(summ):
            if re.search(r"\d{4,8}", body) or _SMS_FIELD_RE.search(summ):
                return "pass", "sms_field_input"
            # 设备回执会把验证码收成「输入 6 字」，位数与本次要填的码一致即准出。
            if re.fullmatch(r"\d{4,8}", exp) and re.search(
                rf"输入\s*{len(exp)}\s*字", summ
            ):
                return "pass", "sms_field_input"
            return "inconclusive", "sms_input_without_otp_digits"
        if _SMS_FIELD_RE.search(summ) and re.search(r"\d{4,8}", body):
            return "pass", "sms_field_input"
        return "inconclusive", "sms_summary_unverified"
    if fld in ("email", "login_email"):
        if not summ:
            return "inconclusive", "empty_summary"
        if "@" in summ or "email" in summ.lower():
            return "pass", "email_field_input"
        return "inconclusive", "email_summary_unverified"
    if not summ:
        return "inconclusive", "empty_summary"
    return "pass", "generic_input"


def coalesce_input_text_device_status(
    device_status: str,
    *,
    summary: str,
    field: str = "",
    params: dict[str, Any] | None = None,
) -> str:
    """设备 HTTP status 与 summary 准出对齐，避免 pass + exit fail 分裂。"""
    p = params or {}
    field_value = p.get("_field_value") if p.get("_field_value_read") else None
    exit_st, _ = device_input_text_exit(
        summary=summary,
        field=field,
        field_value=field_value if isinstance(field_value, str) or field_value is None else str(field_value),
        expected=str(p.get("text") or ""),
    )
    st = str(device_status or "").strip().lower()
    if exit_st == "fail":
        return "fail"
    if exit_st == "pass" and st in ("pass", "done"):
        return "pass"
    if exit_st == "inconclusive":
        return device_status
    return device_status


def log_milestone_exit_eval(
    writer: Any,
    *,
    milestone_id: str,
    exit_status: str,
    reason: str,
    capability_id: str = "",
    would_block: bool = False,
    evidence: str = "",
) -> None:
    if writer is None:
        return
    writer.append(
        "milestone/exit_eval",
        {
            "milestone_id": str(milestone_id or ""),
            "exit_status": str(exit_status or ""),
            "reason": str(reason or "")[:240],
            "capability_id": str(capability_id or ""),
            "would_block": bool(would_block),
            "evidence": str(evidence or "")[:400],
        },
    )


def hook_device_row(row: dict[str, Any]) -> bool:
    if str(row.get("kind") or "").strip().lower() != "hook":
        return False
    hook = str(row.get("hook_cap") or "").strip()
    device = str(row.get("device_cap") or "").strip()
    return bool(hook and device and hook != device)


def milestone_device_exit_ok(
    row: dict[str, Any],
    *,
    capability_id: str,
    field: str,
    summary: str,
    params: dict[str, Any] | None = None,
) -> tuple[bool, str, str]:
    """hook+device 里程碑是否允许标 pass。"""
    mid = str(row.get("id") or "")
    cap = str(capability_id or "").strip()
    if hook_device_row(row):
        device = str(row.get("device_cap") or "").strip()
        if cap != device:
            return False, "fail", "hook_device_requires_device_cap"
        p = params or {}
        field_value = p.get("_field_value") if p.get("_field_value_read") else None
        st, reason = device_input_text_exit(
            summary=summary,
            field=field,
            field_value=field_value if isinstance(field_value, str) or field_value is None else str(field_value),
            expected=str(p.get("text") or ""),
        )
        if st != "pass":
            return False, st, reason
        if str(row.get("id") or "") == "otp_fill" and device == "input_text":
            if not row.get("otp_ready"):
                return False, "inconclusive", "otp_ready_missing"
        if str(row.get("id") or "") == "account_fill" and device == "input_text":
            if not row.get("lease_ready"):
                return False, "inconclusive", "lease_ready_missing"
        return True, "pass", reason
    if cap == "input_text":
        p = params or {}
        field_value = p.get("_field_value") if p.get("_field_value_read") else None
        st, reason = device_input_text_exit(
            summary=summary,
            field=field,
            field_value=field_value if isinstance(field_value, str) or field_value is None else str(field_value),
            expected=str(p.get("text") or ""),
        )
        return st == "pass", st, reason
    return True, "pass", "non_hook_row"


def evaluate_login_block_exit_shadow(
    cursor: Any,
    ctx: Any,
    *,
    writer: Any = None,
) -> bool:
    """do→check 前评估登录块。返回 True 表示拦住流转。"""
    from mino_nexus.loop.milestones import login_flow_under_milestones, read_state

    if not login_flow_under_milestones(cursor):
        return False
    state = read_state(cursor)
    ref = state.get("block_ref") if isinstance(state.get("block_ref"), dict) else {}
    block_id = str(ref.get("block_id") or "")
    ms = state.get("milestones") if isinstance(state.get("milestones"), list) else []
    issues: list[str] = []
    for row in ms:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "")
        if rid == "otp_fill" and hook_device_row(row):
            ev = str(row.get("evidence") or "")
            if str(row.get("status") or "") == "pass":
                if ev != "input_text:sms_code":
                    issues.append("otp_fill_pass_without_sms_evidence")
                elif str(row.get("exit_eval") or "") != "pass":
                    issues.append("otp_fill_exit_eval_not_pass")
            elif not row.get("optional"):
                issues.append("otp_fill_incomplete")
    would_block = bool(issues)
    if writer:
        writer.append(
            "block/exit_eval",
            {
                "block_id": block_id,
                "exit_status": "fail" if would_block else "pass",
                "would_block": would_block,
                "issues": issues[:8],
                "phase": "do",
            },
        )
    return would_block
