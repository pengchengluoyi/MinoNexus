"""步骤「业务意图」合同：不以 cap 次数/动作族次数收工，以意图是否达成为准。"""
from __future__ import annotations

import re
from typing import Any

_INTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("legal_consent", re.compile(r"同意|协议|勾选|隐私政策|用户协议", re.I)),
    ("logout", re.compile(r"退出登录|注销|登出", re.I)),
    ("login_flow", re.compile(r"验证码|短信|OTP|输入.{0,8}手机|填写.{0,8}手机|密码|一键登录|登录成功", re.I)),
    ("login_entry", re.compile(r"手机号登录|去登录|登录按钮|点击.{0,12}登录", re.I)),
    ("like", re.compile(r"点赞|喜欢", re.I)),
    ("open_app", re.compile(r"打开|启动|冷启动|进入应用", re.I)),
    ("clear_cache", re.compile(r"清缓存|清除缓存|清理缓存", re.I)),
    ("nav_tab", re.compile(r"底部|Tab|标签|「我的」|我的页", re.I)),
    ("swipe_gesture", re.compile(r"上滑|下滑|左滑|右滑|滑动", re.I)),
    ("input_fill", re.compile(r"输入|填写|填入", re.I)),
]

_TAP_MARKS_INTENT: list[tuple[str, re.Pattern[str]]] = [
    ("login_entry", re.compile(r"手机号登录|去登录|登录按钮|点击.{0,12}登录", re.I)),
    ("nav_tab", re.compile(r"底部|Tab|标签|「我的」|我的页", re.I)),
    ("like", re.compile(r"点赞|喜欢", re.I)),
    ("logout", re.compile(r"退出登录|注销|登出", re.I)),
]

_CAP_INTENT: dict[str, str] = {
    "accept_legal_consent": "legal_consent",
    "request_sms_code": "sms_send",
    "get_otp": "otp_fill",
    "lease_account": "account_lease",
    "clear_app_cache": "clear_cache",
    "system_pkg_clear": "clear_cache",
    "launch_app": "open_app",
    "open_app": "open_app",
    "swipe_direction": "swipe_gesture",
    "swipe_element_to_element": "swipe_gesture",
    "input_text": "input_fill",
    "fsm_navigate": "nav_tab",
}

_STRUCTURAL_CAPS = frozenset(
    {
        "accept_legal_consent",
        "request_sms_code",
        "dismiss_ime",
        "get_otp",
        "clear_app_cache",
        "system_pkg_clear",
    }
)


def mark_tap_intents_from_instruction(instruction: str, intents_done: set[str]) -> None:
    """tap_element 成功时，标记可由「点一下」完成的意图。"""
    text = str(instruction or "")
    for name, pat in _TAP_MARKS_INTENT:
        if pat.search(text):
            intents_done.add(name)


def cap_step_intent(cap_id: str, *, params: dict[str, Any] | None = None) -> str:
    cap = str(cap_id or "").strip()
    base = str(_CAP_INTENT.get(cap) or "")
    if cap == "input_text":
        field = str((params or {}).get("field") or "").lower()
        if field in ("phone",):
            return "login_phone"
        if field in ("sms_code", "验证码"):
            return "otp_fill"
    if cap == "tap_element" and base:
        return base
    return base


def instruction_required_intents(instruction: str) -> set[str]:
    text = str(instruction or "").strip()
    if not text:
        return set()
    out: set[str] = set()
    for name, pat in _INTENT_PATTERNS:
        if pat.search(text):
            out.add(name)
    return out


_INTENT_LABELS: dict[str, str] = {
    "legal_consent": "勾选/同意协议",
    "logout": "退出登录",
    "login_flow": "登录流程（发码/填码/登录成功）",
    "login_entry": "进入登录",
    "like": "点赞",
    "open_app": "打开应用",
    "clear_cache": "清缓存",
    "nav_tab": "切换 Tab/导航",
    "swipe_gesture": "滑动",
    "input_fill": "输入内容",
    "sms_send": "发送验证码",
    "login_phone": "填写手机号",
    "otp_fill": "填写验证码",
}


def _expand_intents_done(done: set[str]) -> set[str]:
    """组合意图：登录流程子项齐则视为 login_flow 达成。"""
    out = set(done)
    if "login_flow" not in out:
        sub = {"login_phone", "sms_send", "otp_fill"}
        if sub.issubset(out) or ({"sms_send", "otp_fill"}.issubset(out) and "login_phone" in out):
            out.add("login_flow")
        elif "otp_fill" in out and "sms_send" in out:
            out.add("login_flow")
    return out


def format_intent_progress(
    *,
    instruction: str,
    intents_done: set[str] | None,
) -> str:
    """require_do_work / correction 用：明确已完成 vs 未完成意图。"""
    need = instruction_required_intents(instruction)
    done = _expand_intents_done({str(x) for x in (intents_done or set()) if str(x)})
    if not need:
        return ""
    done_h = [ _INTENT_LABELS.get(k, k) for k in sorted(need & done) ]
    miss_h = [ _INTENT_LABELS.get(k, k) for k in sorted(need - done) ]
    parts = []
    if done_h:
        parts.append(f"已完成意图：{'、'.join(done_h)}")
    if miss_h:
        parts.append(f"未完成意图：{'、'.join(miss_h)}")
    elif need.issubset(done):
        parts.append("本步所需意图均已达成")
    return "；".join(parts)


def step_intents_satisfied(
    *,
    instruction: str,
    intents_done: set[str] | None,
) -> tuple[bool, str]:
    need = instruction_required_intents(instruction)
    done = _expand_intents_done({str(x) for x in (intents_done or set()) if str(x)})
    if not need:
        return True, ""
    missing = sorted(need - done)
    if not missing:
        return True, ""
    prog = format_intent_progress(instruction=instruction, intents_done=intents_done)
    return False, prog or f"尚有未完成意图：{', '.join(missing)}"


def intent_micro_progress_line(instruction: str) -> str:
    need = instruction_required_intents(instruction)
    if not need:
        return ""
    labels = {
        "legal_consent": "同意协议",
        "logout": "退出登录",
        "login_flow": "登录相关",
        "like": "点赞",
        "open_app": "打开应用",
        "clear_cache": "清缓存",
        "nav_tab": "Tab/导航",
        "swipe_gesture": "滑动",
        "input_fill": "输入",
    }
    bits = [labels.get(k, k) for k in sorted(need)]
    return f"【本步意图】须达成：{'、'.join(bits)}（以业务结果为准，非单纯 cap 次数）"


def is_structural_cap(cap_id: str) -> bool:
    return str(cap_id or "").strip() in _STRUCTURAL_CAPS
