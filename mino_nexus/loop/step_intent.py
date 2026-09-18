"""步骤「业务意图」合同：不以 cap 次数/动作族次数收工，以意图是否达成为准。"""
from __future__ import annotations

import re
from typing import Any

_INTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("legal_consent", re.compile(r"同意|协议|勾选|隐私政策|用户协议", re.I)),
    ("logout", re.compile(r"退出登录|注销|登出", re.I)),
    ("login_flow", re.compile(r"验证码|短信|OTP|输入.{0,8}手机|填写.{0,8}手机|密码|一键登录|登录成功", re.I)),
    ("login_entry", re.compile(r"手机号登录|去登录|登录按钮|点击(?!.*退出).{0,12}登录", re.I)),
    ("like", re.compile(r"点赞|喜欢", re.I)),
    ("open_app", re.compile(r"打开|启动|冷启动|进入应用", re.I)),
    ("clear_cache", re.compile(r"清缓存|清除缓存|清理缓存", re.I)),
    ("nav_tab", re.compile(r"底部|Tab|标签|「我的」|我的页", re.I)),
    ("open_content", re.compile(r"帖子|详情|卡片|动态|笔记|feed|Feed", re.I)),
    ("swipe_gesture", re.compile(r"上滑|下滑|左滑|右滑|滑动|向[上下左右]滑", re.I)),
    ("input_fill", re.compile(r"输入|填写|填入", re.I)),
]

_TAP_MARKS_INTENT: list[tuple[str, re.Pattern[str]]] = [
    ("login_entry", re.compile(r"手机号登录|去登录|登录按钮|点击(?!.*退出).{0,12}登录", re.I)),
    ("nav_tab", re.compile(r"底部|Tab|标签|「我的」|我的页", re.I)),
    ("open_content", re.compile(r"帖子|详情|卡片|进入详情", re.I)),
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
    """tap_element 成功时，标记可由「点一下」完成的意图（不含需二次确认的退出登录）。"""
    text = str(instruction or "")
    if _logout_needs_confirm(text):
        return
    for name, pat in _TAP_MARKS_INTENT:
        if pat.search(text):
            intents_done.add(name)


def _logout_needs_confirm(instruction: str) -> bool:
    text = str(instruction or "")
    return bool(re.search(r"退出登录|注销|登出", text, re.I) and re.search(r"确认|确定", text, re.I))


_LOGOUT_MENU_RE = re.compile(r"退出登录|注销|登出", re.I)
_LOGOUT_CONFIRM_TAP_RE = re.compile(
    r"(?:^|[「『])(?:退出|确定|确认)(?:」』|$)|^(?:退出|确定|确认)$",
    re.I,
)


def mark_tap_intents_from_tap(
    instruction: str,
    *,
    tap_label: str,
    intents_done: set[str],
) -> None:
    """按实际点击文案标记意图（退出登录+确认须点确认钮才算 logout）。"""
    instr = str(instruction or "")
    label = str(tap_label or "").strip()
    if not label:
        mark_tap_intents_from_instruction(instr, intents_done)
        return
    if _logout_needs_confirm(instr):
        if _LOGOUT_MENU_RE.search(label):
            intents_done.add("logout_pending")
            return
        if _LOGOUT_CONFIRM_TAP_RE.search(label):
            intents_done.discard("logout_pending")
            intents_done.add("logout")
        return
    mark_tap_intents_from_instruction(instr, intents_done)


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
    if "login_flow" in out and "input_fill" in out:
        out.discard("input_fill")
    if "logout" in out:
        out.discard("login_flow")
        out.discard("login_entry")
    if _logout_needs_confirm(text) and "logout" in out:
        out.discard("logout_pending")
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
    "open_content": "打开帖子/详情",
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
    if "input_fill" not in out and "login_flow" in out:
        out.add("input_fill")
    if "login_entry" not in out and "login_flow" in out:
        out.add("login_entry")
    if "logout" in out:
        out.discard("logout_pending")
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
