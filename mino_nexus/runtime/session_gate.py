# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""登录态闸门：开业务循环前给出 SessionFact。

场景理解（是不是登录测试）由 CaseScene JSON 给出，这里只钳制枚举。
非登录用例：前置里退出并重新登录，结果写入 RunContext，后续跳过观察态。
登录/退出/注册用例：不自动登录，避免把用例本身做掉。
仅微信一键且没有手机号入口 → untestable。界面树不当结论。
"""
from __future__ import annotations

import re
from typing import Any, Optional

TAG = "SessionGate"

_WECHAT_ONLY_RE = re.compile(
    r"微信(一键)?登录|使用微信登录|微信授权|WeChat\s*Login|打开微信",
    re.I,
)
_PHONE_LOGIN_RE = re.compile(r"手机号|验证码|短信|一键登录|本机号码")
_LOGIN_MODULE_RE = re.compile(r"登录|login", re.I)
_LOGIN_TEST_STEP_RE = re.compile(
    r"登录方式|登录页|游客|一键登录|微信登录|苹果|账号密码|手机号登录|验证码登录|账号登录",
    re.I,
)

_SESSION_PREP = frozenset({"relogin", "logout", "skip"})
_REQUIRED = frozenset({"logged_in", "guest", "any"})
_DEVICE_NEED = frozenset({"app", "web", "app_web", "ab_pair"})
_SCENE_PLATFORM = frozenset({"android", "ios", "web", "any"})
PREP_KINDS = frozenset({
    "clear_cache",
    "check_sim",
    "check_wechat",
    "check_no_wechat",
    "check_ios_device",
    "check_android_device",
    "check_logged_in",
    "check_not_logged_in",
    "check_env",
    "keep_permission_prompt",
    "check_app_foreground",
    "check_app_version",
    "web_config",
    "remote_config",
    "backend_data",
    "sms_live",
    "external_channel",
    "device_mock",
    "unknown",
})
_AFTER_LAUNCH = frozenset({"check_logged_in", "check_not_logged_in", "check_env"})


def split_precondition_text(text: str) -> list[str]:
    """按编号/换行切开前置原文。不猜 kind。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = re.split(r"(?:\n|^)\s*\d+[.、．)\）]\s*", raw, flags=re.M)
    lines = [p.strip() for p in parts if p and p.strip()]
    if len(lines) <= 1 and raw:
        return [raw]
    return lines


def prep_kind_phase(kind: str) -> str:
    return "after_launch" if str(kind or "") in _AFTER_LAUNCH else "before_launch"


def clamp_prep_items(raw: Any, warnings: Optional[list[str]] = None) -> list[dict[str, Any]]:
    notes = warnings if warnings is not None else []
    rows = raw if isinstance(raw, list) else []
    out: list[dict[str, Any]] = []
    for item in rows[:20]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:240]
        if not text:
            continue
        kind_raw = str(item.get("kind") or "").strip().lower()
        if kind_raw in PREP_KINDS:
            kind = kind_raw
        else:
            if kind_raw:
                notes.append(f"prep kind={kind_raw!r} → unknown")
            kind = "unknown"
        phase_raw = str(item.get("phase") or "").strip().lower()
        phase = phase_raw if phase_raw in {"before_launch", "after_launch"} else prep_kind_phase(kind)
        out.append({"text": text, "kind": kind, "phase": phase})
    return out


def clamp_case_scene(raw: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """把模型 JSON 钳成合法枚举。非法或空对象 → skip（不自动登录）、device_need=app。"""
    row = raw if isinstance(raw, dict) else {}
    warnings: list[str] = []
    prep_raw = str(row.get("session_prep") or "").strip().lower()
    req_raw = str(row.get("required_session") or "").strip().lower()
    if prep_raw in _SESSION_PREP:
        prep = prep_raw
    else:
        if prep_raw:
            warnings.append(f"session_prep={prep_raw!r} → skip")
        prep = "skip"
    if req_raw in _REQUIRED:
        req = req_raw
    else:
        if req_raw:
            warnings.append(f"required_session={req_raw!r} → any")
        req = "any"
    if prep == "relogin" and req == "guest":
        warnings.append("relogin 与游客前置冲突 → logout")
        prep = "logout"
    auth = prep != "relogin"
    need_raw = str(row.get("device_need") or "").strip().lower()
    if need_raw in _DEVICE_NEED:
        device_need = need_raw
    else:
        if need_raw:
            warnings.append(f"device_need={need_raw!r} → app")
        elif "device_need" not in row:
            warnings.append("device_need 缺失 → app")
        device_need = "app"
    plat_raw = str(row.get("platform") or "").strip().lower()
    if plat_raw in _SCENE_PLATFORM:
        platform = plat_raw
    else:
        if plat_raw:
            warnings.append(f"platform={plat_raw!r} → android")
        elif "platform" not in row:
            warnings.append("platform 缺失 → android")
        platform = "android"
    if "prep_items" not in row:
        warnings.append("prep_items 缺失")
        prep_items = []
    else:
        prep_items = clamp_prep_items(row.get("prep_items"), warnings)
    reason = str(row.get("reason") or "").strip()[:240]
    if not reason:
        reason = {
            "relogin": "业务用例，前置退出再登录",
            "logout": "登录/游客用例，前置只退出不登录",
            "skip": "退出/切号用例或无法判定，不自动登录",
        }[prep]
    how = "fallback"
    if prep_raw in _SESSION_PREP and req_raw in _REQUIRED and need_raw in _DEVICE_NEED and plat_raw in _SCENE_PLATFORM:
        how = "clamp" if warnings else "llm"
    elif prep_raw or req_raw or need_raw or plat_raw or "prep_items" in row:
        how = "clamp"
    return {
        "session_prep": prep,
        "required_session": req,
        "auth_under_test": auth,
        "device_need": device_need,
        "platform": platform,
        "prep_items": prep_items,
        "reason": reason,
        "how": how,
        "parse_warnings": warnings,
    }


def fallback_case_scene(reason: str = "", precondition: str = "") -> dict[str, Any]:
    items = [
        {"text": line, "kind": "unknown", "phase": "before_launch"}
        for line in split_precondition_text(precondition)
    ]
    row = clamp_case_scene({
        "session_prep": "skip",
        "required_session": "any",
        "device_need": "app",
        "platform": "android",
        "prep_items": items,
        "reason": (reason or "").strip()[:240] or "场景理解不可用，不自动登录",
    })
    row["how"] = "fallback"
    row["reason"] = (reason or "").strip()[:240] or "场景理解不可用，不自动登录"
    return row


def session_prep_intent(
    scene: Optional[dict[str, Any]] = None,
    **_legacy: Any,
) -> str:
    """前置要对登录态做什么：relogin | logout | skip。只认 CaseScene，不再扫关键字。"""
    del _legacy
    return str(clamp_case_scene(scene).get("session_prep") or "skip")


def scene_allows_account_reset(scene: Optional[dict[str, Any]] = None) -> bool:
    """退出/切号用例或前置要清缓存时，允许执行层动账号环境。"""
    row = clamp_case_scene(scene)
    if str(row.get("session_prep") or "") == "skip":
        return True
    return any(str(it.get("kind") or "") == "clear_cache" for it in (row.get("prep_items") or []))


def is_login_related_case(
    scene: Optional[dict[str, Any]] = None,
    **_legacy: Any,
) -> bool:
    """登录/退出/游客环境类用例：前置不要自动登录。没有场景时按相关处理。"""
    del _legacy
    if not scene:
        return True
    return session_prep_intent(scene) != "relogin"


def required_session(precondition: str = "", scene: Optional[dict[str, Any]] = None) -> str:
    """logged_in | guest | any。只认 CaseScene；没有场景时 any，不再扫前置关键字。"""
    del precondition
    if not scene:
        return "any"
    return str(clamp_case_scene(scene).get("required_session") or "any")


def _case_text_blob(case: dict[str, Any]) -> str:
    bits: list[str] = []
    for key in ("name", "module", "precondition", "precondition_raw", "steps_raw", "expected_raw"):
        val = case.get(key)
        if isinstance(val, list):
            bits.extend(str(x) for x in val if x)
        elif val:
            bits.append(str(val))
    steps = case.get("steps")
    if isinstance(steps, list):
        for item in steps:
            if isinstance(item, dict):
                bits.append(str(item.get("text") or item.get("instruction") or ""))
            elif item:
                bits.append(str(item))
    return " ".join(bits)


def is_login_module_case(
    case: Optional[dict[str, Any]] = None,
    scene: Optional[dict[str, Any]] = None,
) -> bool:
    """登录模块 / 登录流程测试：需要 guest 起点，批跑可能继承上一条已登录。"""
    row = clamp_case_scene(scene) if scene else {}
    req = str(row.get("required_session") or "")
    prep = str(row.get("session_prep") or "")
    if req == "guest" or prep == "logout":
        return True
    if not case:
        return False
    module = str(case.get("module") or "").strip()
    if module and _LOGIN_MODULE_RE.search(module):
        return True
    return bool(_LOGIN_TEST_STEP_RE.search(_case_text_blob(case)))


def ensure_case_scene(
    case: dict[str, Any],
    ctx_scene: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """合并 CaseScene JSON；登录模块缺省时补 guest + logout prep。"""
    raw = case.get("case_scene") if isinstance(case.get("case_scene"), dict) else None
    if raw is None and isinstance(case.get("scene"), dict):
        raw = case.get("scene")
    merged: dict[str, Any] = {**(ctx_scene or {}), **(raw or {})}
    pre = str(case.get("precondition") or merged.get("precondition") or "").strip()
    if pre and re.search(r"登录成功|已登录", pre):
        if str(merged.get("required_session") or "any") in ("", "any"):
            merged["required_session"] = "logged_in"
        if str(merged.get("session_prep") or "skip") == "skip":
            merged["session_prep"] = "relogin"
    if (
        str(merged.get("required_session") or "") in _REQUIRED
        and str(merged.get("session_prep") or "") in _SESSION_PREP
    ):
        return clamp_case_scene({**merged, **({"precondition": pre} if pre else {})})
    if is_login_module_case(case, merged):
        return clamp_case_scene({
            **merged,
            "session_prep": "logout",
            "required_session": "guest",
            "device_need": merged.get("device_need") or "app",
        })
    if merged:
        return clamp_case_scene({**merged, "precondition": pre} if pre else merged)
    return fallback_case_scene(precondition=pre)


def compile_otp_prep_hint(
    *,
    accounts_brief: str = "",
    session_block: str = "",
    hierarchy_text: str = "",
    has_get_otp: bool = False,
) -> str:
    """前置：已租号且处于验证码页时，引导 get_otp 而非盲填固定码。"""
    brief = str(accounts_brief or "").strip()
    if not brief or "未租" in brief or brief.startswith("（未租"):
        return ""
    blob = f"{session_block}\n{hierarchy_text}"
    if not re.search(r"验证码|短信|OTP|sms", blob, re.I):
        return ""
    if has_get_otp:
        return (
            "【验证码】已租号且屏上为验证码/短信流程：优先 get_otp 取码填入，"
            "禁止盲填知识库固定码；取码失败再 signal_ask_human。"
        )
    return (
        "【验证码】已租号且屏上为验证码流程：勿重复 input_text 固定码，"
        "请 signal_ask_human 获取验证码。"
    )


def format_required_session_brief(scene: Optional[dict[str, Any]] = None) -> str:
    """inspect-session 的 required_session 槽：结构化要求，不是前置原文。"""
    req = required_session(scene=scene)
    if req == "guest":
        return (
            "本条为登录流程测试，需要未登录或完整登录页（guest）。"
            "批跑可能继承上一条已登录会话；若当前已登录，next 应为 logout。"
        )
    if req == "logged_in":
        return "本条需要已登录态（logged_in）。"
    return "（未限定会话要求，记录当前屏即可）"


def reconcile_inspect_session(row: dict[str, Any], *, required: str = "any") -> dict[str, Any]:
    """程序侧钳制：guest 用例 + 已登录 + keep → 改为 logout（配合 inspect-session prompt）。"""
    if not row.get("ok"):
        return row
    req = str(required or "any").strip().lower()
    session = str(row.get("session") or "unknown").strip().lower()
    nxt = str(row.get("next") or "keep").strip().lower()
    if req == "guest" and session == "logged_in" and nxt == "keep":
        reason = str(row.get("reason") or "").strip()
        extra = "用例要求未登录/登录页，当前已登录，应先退出再测"
        out = dict(row)
        out["next"] = "logout"
        out["reason"] = f"{reason}；{extra}" if reason else extra
        return out
    return row


def compile_login_session_hint(
    scene: Optional[dict[str, Any]] = None,
    session_block: str = "",
) -> str:
    """B3：编译进 checkpoints_block，与 agent-decide 登录态块配合。"""
    if not is_login_module_case(scene=scene):
        return ""
    block = str(session_block or "")
    if "session=logged_in" in block or "next=logout" in block:
        return (
            "【登录态】当前已登录或须退出：先退出至登录页，再执行本步「点登录方式」类操作；"
            "勿在「我的」/个人主页上找登录入口；勿用 close_app 循环代替退出。"
        )
    if required_session(scene=scene) == "guest":
        return (
            "【登录态】本用例测登录流程，起点应为未登录或完整登录页；"
            "若屏幕为个人主页/昵称页，先退出再点登录方式。"
        )
    return ""


def is_wechat_untestable(screen_text: str) -> bool:
    blob = str(screen_text or "")
    if not _WECHAT_ONLY_RE.search(blob):
        return False
    if _PHONE_LOGIN_RE.search(blob) and not re.search(r"仅(能|可)微信|必须微信", blob):
        return False
    return True


def observe_session(
    *,
    sn: str = "",
    platform: str = "",
    package: str = "",
    required: str = "any",
    screen_text: str = "",
    inspect_row: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """登录态只看图。界面树 / 底栏 / shared_prefs 不当结论，也不再去设备上扒。"""
    del sn, platform, package
    row = inspect_row if isinstance(inspect_row, dict) else {}
    inspect_ok = bool(row.get("ok"))
    vlm_session = str(row.get("session") or "").strip().lower() if inspect_ok else ""
    identity = str(row.get("identity") or "unknown").strip().lower() if inspect_ok else "unknown"
    seen = str(row.get("seen") or "")[:240]
    blob = seen or str(screen_text or "")

    observed = "unknown"
    how = "unknown"
    reason = str(row.get("reason") or "")
    if vlm_session == "logged_in":
        observed = "logged_in"
        how = "vlm"
        reason = reason or "看图判定已登录"
    elif vlm_session == "logged_out":
        observed = "guest"
        how = "vlm"
        reason = reason or "看图判定未登录"
    else:
        how = "vlm" if inspect_ok else "unknown"
        reason = reason or "看图无法确认登录态"

    wx_blob = f"{seen} {reason}" if inspect_ok else ""
    return {
        "required": required,
        "observed": observed,
        "identity": identity if identity in {"match", "mismatch", "unknown"} else "unknown",
        "how": how,
        "screen_text": blob[:800],
        "reason": reason[:240],
        "seen": seen,
        "wechat_untestable": bool(
            inspect_ok and observed != "logged_in" and is_wechat_untestable(wx_blob)
        ),
        "inspect_ok": inspect_ok,
    }


def evaluate_gate(fact: dict[str, Any]) -> dict[str, Any]:
    """返回 {ok, status, category, reason}。status 为 pass|fail|untestable。"""
    required = str(fact.get("required") or "any")
    observed = str(fact.get("observed") or "unknown")
    if required == "any":
        return {"ok": True, "status": "pass", "category": "", "reason": ""}
    if required == "logged_in":
        if observed == "logged_in":
            return {"ok": True, "status": "pass", "category": "", "reason": ""}
        if fact.get("wechat_untestable"):
            reason = "需要已登录，当前是微信登录页，无法自动化（untestable）"
            return {
                "ok": False,
                "status": "untestable",
                "category": "goal_unreachable",
                "reason": reason,
            }
        reason = fact.get("reason") or "需要已登录，当前不是已登录态"
        if observed == "guest" or "登录" in str(reason):
            reason = f"需要已登录，当前是登录页/游客。{reason}".strip()
        return {
            "ok": False,
            "status": "fail",
            "category": "goal_unreachable",
            "reason": reason[:240],
        }
    if required == "guest":
        if observed == "guest":
            return {"ok": True, "status": "pass", "category": "", "reason": ""}
        if observed == "logged_in":
            return {
                "ok": False,
                "status": "fail",
                "category": "goal_unreachable",
                "reason": "需要游客/未登录，当前仍是已登录；会话对齐未能退出",
            }
        return {
            "ok": False,
            "status": "fail",
            "category": "goal_unreachable",
            "reason": fact.get("reason") or "无法确认未登录",
        }
    return {"ok": True, "status": "pass", "category": "", "reason": ""}


def can_reuse_task_session(
    *,
    required: str = "any",
    task_session: Optional[dict[str, Any]] = None,
    picked_phone: str = "",
    dirty: bool = False,
) -> bool:
    """同设备、同账号、中间没有清缓存/杀进程时，不必再看图验登录。

    不跳过产品预期。只跳过前置里的 inspect_session。
    业务用例沿用的是前置重新登录写入的 RunContext，不是「看过一次图」。
    """
    if dirty:
        return False
    sess = task_session if isinstance(task_session, dict) else {}
    observed = str(sess.get("observed") or "").strip().lower()
    logged_in = bool(sess.get("logged_in")) or observed == "logged_in"
    want = str(required or "any").strip().lower() or "any"
    phone = re.sub(r"\s+", "", str(picked_phone or ""))
    prev = re.sub(r"\s+", "", str(sess.get("phone") or ""))
    if phone and prev and phone != prev:
        return False
    if want == "logged_in":
        return logged_in
    if want == "guest":
        return observed == "guest" or (not logged_in and observed in {"guest", "logged_out"})
    if want == "any":
        return bool(observed and observed != "unknown")
    return False
