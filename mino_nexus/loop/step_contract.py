"""用例单步：instruction 动作族 + 次数合同。"""
from __future__ import annotations

import re
from typing import Any

_OPEN_RE = re.compile(r"打开|启动|进入应用|冷启动", re.I)
_TAP_RE = re.compile(r"点击|点按|轻触|选中|勾选", re.I)
_SWIPE_RE = re.compile(r"上滑|下滑|左滑|右滑|滑动|swipe", re.I)
_INPUT_RE = re.compile(r"输入|填写|填入", re.I)
_CLEAR_RE = re.compile(r"清缓存|清除缓存|清理缓存|clear.?cache", re.I)
_LOGIN_FLOW_RE = re.compile(
    r"登录|验证码|短信|手机号|密码|一键登录|发码|获取验证码|sms|otp",
    re.I,
)

_CAP_FAMILY: dict[str, str] = {
    "launch_app": "open",
    "close_app": "open",
    "kill_app": "open",
    "clear_app_cache": "clear",
    "system_pkg_clear": "clear",
    "tap_element": "tap",
    "multi_tap": "tap",
    "long_press_element": "tap",
    "fsm_navigate": "tap",
    "accept_legal_consent": "tap",
    "request_sms_code": "tap",
    "swipe_direction": "swipe",
    "swipe_element_to_element": "swipe",
    "input_text": "input",
    "press_key": "key",
}


def cap_action_family(cap_id: str) -> str:
    return str(_CAP_FAMILY.get(str(cap_id or "").strip(), "") or "")


def instruction_action_families(instruction: str) -> set[str]:
    """从步骤原文推断需要几类设备动作（不含纯等待/观察）。"""
    text = str(instruction or "").strip()
    if not text:
        return set()
    out: set[str] = set()
    if _OPEN_RE.search(text):
        out.add("open")
    if _TAP_RE.search(text):
        out.add("tap")
    if _SWIPE_RE.search(text):
        out.add("swipe")
    if _INPUT_RE.search(text):
        out.add("input")
    if _CLEAR_RE.search(text):
        out.add("clear")
    return out


def instruction_action_counts(instruction: str) -> dict[str, int]:
    """同族多次动词计数；分句统计「点击」避免漏 intent。"""
    text = str(instruction or "").strip()
    fams = instruction_action_families(text)
    out: dict[str, int] = {}
    if "tap" in fams:
        clauses = re.split(r"[，,；;]|并且|然后|再", text)
        tap_n = sum(len(_TAP_RE.findall(c)) for c in clauses if c.strip())
        if tap_n < 1:
            tap_n = len(_TAP_RE.findall(text))
        out["tap"] = max(1, tap_n)
    if "swipe" in fams:
        n = len(_SWIPE_RE.findall(text))
        out["swipe"] = max(1, n)
    if "input" in fams:
        n = len(_INPUT_RE.findall(text))
        out["input"] = max(1, n)
    if "open" in fams:
        out["open"] = 1
    if "clear" in fams:
        out["clear"] = 1
    return out


def instruction_micro_progress_line(instruction: str) -> str:
    """写进 checkpoints_block 的子动作进度说明。"""
    counts = instruction_action_counts(instruction)
    if not counts:
        return ""
    bits = []
    labels = {"tap": "点击", "swipe": "滑动", "input": "输入", "open": "打开", "clear": "清缓存"}
    for key in ("open", "clear", "swipe", "input", "tap"):
        n = int(counts.get(key) or 0)
        if n > 0:
            bits.append(f"{labels.get(key, key)}×{n}")
    if not bits:
        return ""
    return f"【本步子动作】须完成：{'、'.join(bits)}（每次 pass 的设备操作计 1 次，收工前须达标）"


def instruction_allows_login_flow(
    instruction: str,
    *,
    login_module_case: bool = False,
) -> bool:
    if login_module_case:
        return True
    return bool(_LOGIN_FLOW_RE.search(str(instruction or "")))


def step_actions_satisfied(
    *,
    instruction: str,
    families_done: set[str] | None,
    family_counts: dict[str, int] | None = None,
) -> tuple[bool, str]:
    """是否已覆盖 instruction 声明的动作族与次数。"""
    need = instruction_action_families(instruction)
    done = {str(x) for x in (families_done or set()) if str(x)}
    if need:
        missing = sorted(need - done)
        if missing:
            return False, f"步骤还要求：{', '.join(missing)}（已完成：{', '.join(sorted(done)) or '无'}）"
    need_counts = instruction_action_counts(instruction)
    counts = dict(family_counts or {})
    short = []
    for key, need_n in sorted(need_counts.items()):
        got = int(counts.get(key) or 0)
        if got < need_n:
            labels = {"tap": "点击", "swipe": "滑动", "input": "输入", "open": "打开", "clear": "清缓存"}
            short.append(f"{labels.get(key, key)} {got}/{need_n}")
    if short:
        return False, f"本步操作次数不足：{', '.join(short)}"
    return True, ""


def precondition_requires_clear_cache(precondition: str) -> bool:
    return bool(_CLEAR_RE.search(str(precondition or "")))
