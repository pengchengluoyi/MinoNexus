"""用例步骤指针：prep → do → check。不含原文关键字分流。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Optional

from mino_nexus.ai.case_text import parse_numbered_items_rules
from mino_nexus.loop.fuse.action_fuse import ProgressGate
from mino_nexus.services.run_store import spec_lines

_POINTER_TEMPLATES: dict[str, str] = {
    "prep_header": "【执行纪律：先完成前置检查，禁止进入操作步骤，禁止校验预期。】",
    "prep_current": "【当前只做前置】{precondition}",
    "prep_tail": (
        "前置只做三件事：筛选账号（lease_account）、筛选设备、环境清理（clear_app_cache 等）。"
        "禁止 check_run_env / 切换测试环境（环境由批次 env_profile 确定）。"
        "禁止重复 read_device_data（history 已有 sim=READY 即满足）。"
        "前置满足后立刻 signal_done。锁屏/黑屏用 recover_*，不要用 prep 工具唤醒。"
        "禁止进步骤、禁止验预期。"
    ),
    "do_header": (
        "【执行纪律：严格按步骤编号。禁止跳到后面的步骤，禁止提前验后面的预期。】"
        "do 阶段只执行【当前只做步骤 n】中的 instruction；"
        "「达成信号/预期」仅供判断可否 signal_done，禁止把校验预期当成操作目标。"
    ),
    "do_current": "【当前只做步骤 {n}】{instruction}\n【本步达成信号】{achievement}",
    "do_operation": (
        "【当前只做步骤 {n}·操作】{instruction}\n"
        "【达成信号·摘要】{achievement_brief}（仅供收工判断，禁止当作点击/输入目标）"
    ),
    "do_achievement": (
        "【当前只做步骤 {n}·收工】达成信号：{achievement}\n"
        "若屏上已满足且无其它待办，请 signal_done；禁止再改界面凑预期。"
    ),
    "do_tail": (
        "子动作全部完成且达成信号满足后调 signal_done（不是整案结束）。"
        "跳转类步骤成功后入口从屏上消失是正常现象，禁止因此 signal_give_up。"
        "本步未要求登录/验证码时：勿展开完整登录链；若 expected 已满足请立刻 signal_done。"
        "禁止去做后面步骤。"
    ),
    "do_tail_login_module": (
        "做完本步操作后调 signal_done，表示本步操作结束（不是整案结束）。"
        "达成信号一旦在屏上出现，即视为本步完成，立刻 signal_done。"
        "业务入口弹出登录弹窗时先完成登录，禁止只关弹窗反复点同一入口。禁止去做后面步骤。"
    ),
    "do_no_expected": "【本步无预期】做完操作即视为完成，不要试图找校验依据。",
    "check_current": "【当前只验步骤 {n}】{expected}",
    "check_tail": "只看当前截图判断预期。调 assert_visual；通过后再 signal_done。禁止点击、滑动、输入来改界面凑绿。",
    "step_future": "[ ] 步骤 {n} 未到，禁止执行：{instruction}",
    "step_done": "[x] 步骤 {n} 已完成：{instruction}",
    "step_active_do": "[>] 步骤 {n} 操作中：{instruction} ｜ 达成信号：{achievement}",
    "step_active_check": "[>] 步骤 {n} 校验中：{instruction} ｜ 预期：{expected}",
    "step_pending_check": "[ ] 步骤 {n} 未到，禁止执行：{instruction} ｜ 做完后校验",
    "step_unlock_hint": "【进度】当前第 {cur}/{total} 步；尚未到达的步骤不在本块展示。",
    "step_achievement_pending": "（本步子动作未完成，达成信号待解锁）",
    "all_done": "全部步骤已完成。不要再操作设备。",
}


def _pointer_templates() -> dict[str, str]:
    return _POINTER_TEMPLATES


def _achievement_label(expected: str) -> str:
    exp = str(expected or "").strip()
    return exp if exp else "本步无预期（做完即过）"


def _expected_defers_to_check(expected: str) -> bool:
    """expected 描述的是登录后/跳转后的结果，不应在 do 阶段空等。"""
    exp = str(expected or "").strip()
    if not exp:
        return False
    markers = (
        "登录成功",
        "进入",
        "跳转到",
        "跳转",
        "生成中",
        "提单",
        "返回",
        "未跳转",
    )
    return any(m in exp for m in markers)


def _do_phase_achievement_brief(instruction: str, expected: str) -> str:
    exp = str(expected or "").strip()
    if not exp:
        return "本步无预期（做完即过）"
    if _expected_defers_to_check(exp):
        return (
            "按 instruction 完成全部子动作后即可 signal_done；"
            f"「{exp[:48]}{'…' if len(exp) > 48 else ''}」仅在 check 阶段 assert_visual 校验，"
            "do 阶段勿为加载/下一页空等。"
        )
    return exp if len(exp) <= 72 else exp[:69].rstrip() + "…"


def _pt(key: str, **kwargs: Any) -> str:
    tpl = _pointer_templates().get(key) or key
    try:
        return str(tpl).format(**kwargs)
    except (KeyError, ValueError):
        return str(tpl)

# agent 决策坐标是 0–1000 千分比；10‰ ≈ 1280 宽屏 13px，仅视为同一触点。
TAP_REPEAT_TOL_MILLI = 10
_OBSERVE_PREFIXES = ("查看", "观察", "确认屏", "检查屏", "目视", "看", "等待")
_WAIT_THEN_ACT_RE = re.compile(r"等待.{0,16}(后|再).{0,12}(点击|输入|滑动|打开|进入|勾选)")


def enrich_assert_expectation(instruction: str, expected: str) -> str:
    """登录类步骤：收紧 VLM 校验措辞，减少密码页/验证码页混判。"""
    exp = str(expected or "").strip()
    instr = str(instruction or "").strip()
    blob = f"{instr}\n{exp}"
    if not exp:
        return exp
    if "验证码" in exp or "发送验证码" in blob:
        if "密码" not in exp:
            return (
                f"{exp}（必须已进入短信验证码输入/发送流程；"
                f"仍停留在账号密码登录页、仅填手机号未发码 → 不通过）"
            )
    if re.search(r"手机号登录|短信登录|验证码登录", blob) and re.search(
        r"切换|进入.*登录页|登录方式", exp
    ):
        if "密码" not in exp and "验证码" not in exp:
            return (
                f"{exp}（须为手机号/短信验证码登录入口或流程；"
                f"主流程为账号+密码登录页 → 不通过）"
            )
    return exp


_GUEST_STEP_RE = re.compile(r"游客|访客")
_GUEST_ENTRY_RE = re.compile(r"游客|访客")
_LOGIN_ENTRY_RE = re.compile(
    r"手机号|"
    r"微信(?:登录|登陆|授权)?|"
    r"(?:去|立即|点击)?登录|"
    r"一键登录|"
    r"账号密码|"
    r"Sign in with Apple|"
    r"(?:通过)?Apple.{0,12}登录|"
    r"苹果.{0,8}(?:登录|账号|授权|ID)",
    re.I,
)


def is_guest_entry_step(instruction: str) -> bool:
    return bool(_GUEST_STEP_RE.search(str(instruction or "")))


def tap_summary_is_guest_entry(summary: str) -> bool:
    return bool(_GUEST_ENTRY_RE.search(str(summary or "")))


def tap_summary_is_login_entry(summary: str) -> bool:
    text = str(summary or "")
    if _GUEST_ENTRY_RE.search(text):
        return False
    return bool(_LOGIN_ENTRY_RE.search(text))


def tap_params_guard_summary(params: dict[str, Any] | None) -> str:
    """Guard 判定登录入口时只用 tap 参数，勿用 LLM thought（易误含「登录」字样）。"""
    p = params if isinstance(params, dict) else {}
    parts = [
        str(p.get("selector_text") or "").strip(),
        str(p.get("text") or "").strip(),
        str(p.get("content_desc") or "").strip(),
    ]
    target = p.get("target")
    if isinstance(target, dict):
        parts.append(str(target.get("text") or "").strip())
        parts.append(str(target.get("content_desc") or "").strip())
    return " ".join(x for x in parts if x).strip()


def compile_guest_entry_hint(instruction: str, *, entry_tapped: bool) -> str:
    if not is_guest_entry_step(instruction):
        return ""
    if entry_tapped:
        return (
            "【游客入口】已点击访客/游客入口；若屏上已是 App 首页或内容页，"
            "请 signal_done 结束本步，勿再点「我的」或登录方式入口。"
        )
    return (
        "【游客入口】本步要点登录页右上角「游客/访客浏览」进入 App；"
        "进入首页后即 signal_done，不要在登录方式之间来回点。"
    )


def is_observe_only_step(instruction: str) -> bool:
    text = str(instruction or "").strip()
    if not text:
        return False
    if _WAIT_THEN_ACT_RE.search(text):
        return False
    return any(text.startswith(prefix) for prefix in _OBSERVE_PREFIXES)


@dataclass
class SeqNode:
    n: int
    instruction: str
    expected: str
    observe_only: bool = False


_NUMBER_PREFIX = re.compile(r"^\s*\d+[.、．)\）]")


def _numbered_column_map(case: dict[str, Any], key: str, raw_key: str) -> dict[int, str]:
    """步骤/预期按编号对齐；缺号留空，禁止用「下一行预期」顶替中间步骤。"""
    raw = str(case.get(raw_key) or "").strip()
    if raw:
        parsed = parse_numbered_items_rules(raw)
        if parsed:
            out: dict[int, str] = {}
            for it in parsed:
                n = int(it.get("num") or 0)
                t = str(it.get("text") or "").strip()
                if n and t:
                    out[n] = t
            if out:
                return out

    items = spec_lines(case.get(key))
    if not items:
        return {}

    by_n: dict[int, str] = {}
    pos = 1
    for line in items:
        text = str(line or "").strip()
        if not text:
            continue
        if _NUMBER_PREFIX.match(text):
            for it in parse_numbered_items_rules(text):
                n = int(it.get("num") or 0)
                t = str(it.get("text") or "").strip()
                if n and t:
                    by_n[n] = t
        else:
            by_n[pos] = text
            pos += 1
    return by_n


CASE_WALL_SEC_PER_UNIT = 30


def count_precondition_units(precondition: str) -> int:
    pre = str(precondition or "").strip()
    if not pre:
        return 0
    parsed = parse_numbered_items_rules(pre)
    if len(parsed) > 1:
        return sum(1 for it in parsed if str(it.get("text") or "").strip())
    if parsed and _NUMBER_PREFIX.search(pre):
        return sum(1 for it in parsed if str(it.get("text") or "").strip())
    return 1


def compute_case_wall_budget_sec(case: dict[str, Any]) -> int:
    """墙钟预算：前置条数×30 + 操作最大步号×30 + 校验步数×30（与操作步数同号）。"""
    prep = count_precondition_units(str(case.get("precondition") or ""))
    nodes = build_seq_nodes(case)
    do_units = max((node.n for node in nodes), default=0)
    check_units = do_units
    units = prep + do_units + check_units
    if units <= 0:
        units = 1
    try:
        override = int(case.get("wall_budget_sec") or 0)
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        return max(CASE_WALL_SEC_PER_UNIT, override)
    return units * CASE_WALL_SEC_PER_UNIT


def build_seq_nodes(case: dict[str, Any]) -> list[SeqNode]:
    """按用例步骤编号建指针：步骤 n 做完才验同号预期。"""
    steps_map = _numbered_column_map(case, "steps", "steps_raw")
    expected_map = _numbered_column_map(case, "expected", "expected_raw")
    if not steps_map and not expected_map:
        name = str(case.get("name") or case.get("case_id") or "").strip()
        if name:
            steps_map = {1: name}
        else:
            return []
    nums = sorted(set(steps_map) | set(expected_map))
    nodes: list[SeqNode] = []
    for n in nums:
        inst = (steps_map.get(n) or "").strip()
        exp = (expected_map.get(n) or "").strip()
        if not inst and not exp:
            continue
        nodes.append(
            SeqNode(
                n=n,
                instruction=inst,
                expected=exp,
                observe_only=is_observe_only_step(inst),
            )
        )
    return nodes


_NAV_CLEAR_CAPS = frozenset({
    "press_key",
    "swipe_direction",
    "swipe_element_to_element",
    "launch_app",
    "close_app",
})


def cap_clears_repeat_tap(cap_id: str) -> bool:
    """导航类操作后允许同坐标重试（误点进子页、返回后重勾等）。"""
    cid = str(cap_id or "").strip()
    if not cid:
        return False
    if cid in _NAV_CLEAR_CAPS or cid.startswith("recover_"):
        return True
    return False


def screen_fingerprint(
    *,
    hierarchy_text: str = "",
    image_base64: str = "",
    width: int = 0,
    height: int = 0,
) -> str:
    """粗粒度界面指纹：用于判断点击后页面是否切换。"""
    parts = [f"{int(width or 0)}x{int(height or 0)}"]
    hier = str(hierarchy_text or "").strip()
    if hier:
        parts.append(hier[:2400])
    elif image_base64:
        blob = str(image_base64)[:8192].encode("utf-8", errors="ignore")
        parts.append(hashlib.sha1(blob).hexdigest()[:16])
    raw = "\n".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def format_skip_repeat_read_device_message(last_summary: str = "") -> str:
    """写入 history，让模型在前置满足后 signal_done。"""
    hint = f"（上次结果：{last_summary[:80]}）" if last_summary else ""
    return (
        f"已拒绝重复 read_device_data{hint}：前置数据已读过且满足要求，"
        f"请直接 signal_done 结束前置；锁屏/黑屏请用 recover_screen_asleep_or_locked，不要用 read_device_data。"
    )


def last_read_device_summary(history_lines: list[str]) -> str:
    prefix = "read_device_data"
    for line in reversed(history_lines or []):
        text = str(line or "")
        if prefix not in text:
            continue
        if "→ pass:" in text:
            return text.split("→ pass:", 1)[-1].strip()
        if "→ skipped:" in text:
            return text.split("→ skipped:", 1)[-1].strip()
    return ""


def read_device_prep_satisfied(summary: str) -> bool:
    blob = str(summary or "").upper()
    if not blob:
        return False
    if "SIM=READY" in blob or "SIM=ABSENT" in blob:
        return True
    if blob.startswith("SIM=") or "SIM_STATE=" in blob:
        return True
    return False


def format_skip_repeat_tap_message(
    last: Optional[dict[str, Any]],
    params: Optional[dict[str, Any]],
) -> str:
    """写入 history，让模型知道同位置再点无效。"""
    p = params or {}
    last = last or {}
    sel = str(p.get("selector_text") or last.get("selector_text") or "").strip()
    if sel:
        target = f"「{sel}」"
    else:
        target = f"({p.get('x')},{p.get('y')})"
    return (
        f"已拒绝重复点击{target}：本步在此位置已点过一次且界面未变，"
        f"再点同位置不会推进步骤；请换元素/坐标，或先处理挡屏（返回、关弹窗、进登录页）。"
    )


def repeats_last_tap(
    last: Optional[dict[str, Any]],
    params: Optional[dict[str, Any]],
    *,
    tol: int = TAP_REPEAT_TOL_MILLI,
    tap_epoch: int = 0,
) -> bool:
    if not last or not params:
        return False
    if int(last.get("epoch") or 0) != int(tap_epoch or 0):
        return False
    try:
        lx, ly = int(last.get("x")), int(last.get("y"))
        nx, ny = int(params.get("x")), int(params.get("y"))
    except (TypeError, ValueError):
        return False
    return abs(lx - nx) <= tol and abs(ly - ny) <= tol


class StepCursor:
    """prep → do → check → 下一步。前置原文不并进步骤指令。"""

    def __init__(self, nodes: list[SeqNode], *, precondition: str = ""):
        self.nodes = list(nodes)
        self.precondition = str(precondition or "").strip()
        self.index = 0
        self.phase = "prep" if self.precondition else "do"
        self.saw_assert = False
        self.step_checked = False
        self.last_tap: Optional[dict[str, Any]] = None
        self.tap_epoch: int = 0
        self.step_ops: int = 0
        self.step_action_families: set[str] = set()
        self.step_family_counts: dict[str, int] = {}
        self.step_intents_done: set[str] = set()
        self.step_structural_caps_done: set[str] = set()
        self.sms_auto_attempted: bool = False
        self.step_goal_met: bool = False
        self.login_module_prompt: bool = False
        self.check_session_refresh: bool = False
        self.advise_recovery_counts: dict[str, int] = {}
        self.recovery_fail_counts: dict[str, int] = {}
        self.login_session_hint: str = ""
        self.otp_prep_hint: str = ""
        self.progress_gate: ProgressGate = ProgressGate()
        self.guest_entry_tapped: bool = False
        self.step_start_fp: str = ""
        self.step_effect_hit_streak: int = 0
        self.step_effect_hint: str = ""
        self.correction_hint: str = ""
        self.prep_session_skip_streak: int = 0
        self.prep_guard_streak: int = 0
        self.require_do_work_streak: int = 0
        self.recovery_block_streak: int = 0
        self.do_subphase: str = "operation"
        self._finish_ctx: Any = None
        self._finish_history: list[str] = []
        self.step_nav_plan_hint: str = ""
        self.step_nav_plan_step_n: int = 0
        self.swipe_stuck_fp: str = ""
        self.swipe_stuck_dir: str = ""
        self.swipe_stuck_count: int = 0
        self.success_criteria_state: dict[str, Any] = {}
        if self.phase != "prep":
            self._sync()

    def current(self) -> Optional[SeqNode]:
        if 0 <= self.index < len(self.nodes):
            return self.nodes[self.index]
        return None

    @property
    def done(self) -> bool:
        return self.phase == "done" or self.current() is None

    def all_uncheckable(self) -> bool:
        return bool(self.nodes) and all(not (n.expected or "").strip() for n in self.nodes)

    def finish_prep(self) -> None:
        self.index = 0
        self.step_checked = False
        self.step_ops = 0
        self.step_action_families = set()
        self.step_family_counts = {}
        self.step_intents_done = set()
        self.step_structural_caps_done = set()
        self.sms_auto_attempted = False
        self.step_goal_met = False
        self.reset_guest_entry()
        self.step_effect_hit_streak = 0
        self.step_effect_hint = ""
        self.correction_hint = ""
        self.prep_session_skip_streak = 0
        self.prep_guard_streak = 0
        self.prep_resource_gate_streak = 0
        self.recovery_block_streak = 0
        self.do_subphase = "operation"
        self.step_nav_plan_hint = ""
        self.step_nav_plan_step_n = 0
        self.swipe_stuck_fp = ""
        self.swipe_stuck_dir = ""
        self.swipe_stuck_count = 0
        self.progress_gate.reset_milestone("do", 1 if self.nodes else 0)
        self._sync()
        self.step_start_fp = ""
        from mino_nexus.loop.milestones import empty_success_criteria, write_state

        write_state(self, empty_success_criteria(self))

    def refresh_step_start_fp(self, fp: str) -> None:
        if self.phase == "do":
            self.step_start_fp = str(fp or "").strip()

    def note_step_effect_hit(self, keywords: list[str], *, expected: str = "") -> None:
        from mino_nexus.loop.step_effect import achievement_hint

        cur = self.current()
        exp = str(expected or "").strip()
        if not exp and cur is not None:
            exp = str(cur.expected or "").strip()
        self.step_effect_hit_streak += 1
        self.step_effect_hint = achievement_hint(keywords, expected=exp)

    def reset_step_effect_streak(self) -> None:
        self.step_effect_hit_streak = 0
        self.step_effect_hint = ""

    def record_step_op(
        self,
        cap_id: str = "",
        *,
        params: dict[str, Any] | None = None,
        count_nav_intent: bool = True,
    ) -> None:
        self.step_ops += 1
        from mino_nexus.loop.step_contract import cap_action_family
        from mino_nexus.loop.step_intent import cap_step_intent, is_structural_cap

        cap = str(cap_id or "").strip()
        intent = cap_step_intent(cap, params=params)
        if intent == "nav_tab" and not count_nav_intent:
            intent = ""
        if intent:
            self.step_intents_done.add(intent)
        if cap == "tap_element":
            from mino_nexus.loop.step_intent import mark_tap_intents_from_tap

            cur = self.current()
            sel = str((params or {}).get("selector_text") or (params or {}).get("text") or "")
            mark_tap_intents_from_tap(
                str(cur.instruction or "") if cur else "",
                tap_label=sel,
                intents_done=self.step_intents_done,
            )
        if cap and is_structural_cap(cap):
            self.step_structural_caps_done.add(cap)
        if cap and not is_structural_cap(cap):
            fam = cap_action_family(cap)
            if fam:
                self.step_action_families.add(fam)
                self.step_family_counts[fam] = int(self.step_family_counts.get(fam) or 0) + 1
                if fam == "swipe":
                    self.step_intents_done.add("swipe_gesture")
        self.refresh_do_subphase()

    def bind_finish_context(self, ctx: Any, history_lines: list[str] | None) -> None:
        self._finish_ctx = ctx
        self._finish_history = [str(x) for x in (history_lines or []) if str(x).strip()][-64:]

    def _login_completion_allows_do_finish(self, instruction: str) -> bool:
        from mino_nexus.loop.login_submit import login_flow_do_may_finish

        ok, msg = login_flow_do_may_finish(
            instruction=instruction,
            history_lines=self._finish_history,
            ctx=self._finish_ctx,
        )
        if not ok and msg:
            self.correction_hint = msg[:280]
        return ok

    def try_auto_finish_do_when_intents_met(self) -> bool:
        """do 阶段业务意图已达成时立即进入 check（或无 expected 时下一步），不等模型 signal_done。"""
        if self.phase != "do":
            return False
        from mino_nexus.loop.milestones import _TERMINAL, read_state

        for row in read_state(self).get("milestones") or []:
            if not isinstance(row, dict) or row.get("optional"):
                continue
            if str(row.get("status") or "pending").strip().lower() not in _TERMINAL:
                return False
        self.refresh_do_subphase()
        if self.do_subphase != "achievement":
            return False
        cur = self.current()
        if not cur:
            return False
        from mino_nexus.loop.step_contract import step_actions_satisfied
        from mino_nexus.loop.step_intent import instruction_required_intents, step_intents_satisfied

        instr = str(cur.instruction or "")
        need_int = instruction_required_intents(instr)
        if need_int:
            ok_int, _ = step_intents_satisfied(
                instruction=instr,
                intents_done=self.step_intents_done,
            )
            if not ok_int:
                return False
        else:
            fam_ok, _ = step_actions_satisfied(
                instruction=instr,
                families_done=self.step_action_families,
                family_counts=self.step_family_counts,
            )
            if not fam_ok:
                return False
        if not self._login_completion_allows_do_finish(instr):
            return False
        from mino_nexus.loop.milestones import _TERMINAL, login_flow_under_milestones, read_state

        if login_flow_under_milestones(self):
            for row in read_state(self).get("milestones") or []:
                if not isinstance(row, dict) or row.get("optional"):
                    continue
                if str(row.get("status") or "pending") not in _TERMINAL:
                    return False
        expected = str(cur.expected or "").strip()
        if expected and int(self.step_effect_hit_streak or 0) <= 0:
            from mino_nexus.loop.vision_plan import _case_from_ctx, _do_plan_locks_append

            case = _case_from_ctx(self._finish_ctx or getattr(self, "run_context", None))
            if not _do_plan_locks_append(self, case):
                return False
        self.step_goal_met = True
        self.require_do_work_streak = 0
        self.correction_hint = ""
        self.enter_check()
        return True

    def try_auto_finish_do_when_milestones_met(self, ctx: Any, *, writer: Any = None) -> bool:
        from mino_nexus.loop.milestones import try_finish_do_if_milestones_complete

        return try_finish_do_if_milestones_complete(self, ctx, writer=writer)

    def refresh_do_subphase(self) -> None:
        if self.phase != "do":
            self.do_subphase = "operation"
            return
        cur = self.current()
        instr = str(cur.instruction or "") if cur else ""
        from mino_nexus.loop.step_contract import step_actions_satisfied
        from mino_nexus.loop.step_intent import instruction_required_intents, step_intents_satisfied

        need_int = instruction_required_intents(instr)
        int_ok, _ = step_intents_satisfied(
            instruction=instr,
            intents_done=self.step_intents_done,
        )
        fam_ok, _ = step_actions_satisfied(
            instruction=instr,
            families_done=self.step_action_families,
            family_counts=self.step_family_counts,
        )
        op_done = int_ok if need_int else fam_ok
        if op_done:
            op_done = self._login_completion_allows_do_finish(instr)
        self.do_subphase = "achievement" if op_done else "operation"

    def bump_advise_recovery(self, rule_id: str) -> int:
        rid = str(rule_id or "").strip()
        n = int(self.advise_recovery_counts.get(rid, 0)) + 1
        self.advise_recovery_counts[rid] = n
        return n

    def bump_recovery_fail(self, rule_id: str) -> int:
        rid = str(rule_id or "").strip()
        n = int(self.recovery_fail_counts.get(rid, 0)) + 1
        self.recovery_fail_counts[rid] = n
        return n

    def bump_recovery_block(self) -> int:
        self.recovery_block_streak = int(self.recovery_block_streak or 0) + 1
        return self.recovery_block_streak

    def clear_recovery_block(self) -> None:
        self.recovery_block_streak = 0

    def mark_guest_entry(self, summary: str = "") -> None:
        if tap_summary_is_guest_entry(summary):
            self.guest_entry_tapped = True

    def reset_guest_entry(self) -> None:
        self.guest_entry_tapped = False

    def _sync(self) -> None:
        cur = self.current()
        if not cur:
            self.phase = "done"
            return
        if not cur.instruction and cur.expected:
            self.phase = "check"
            self.step_checked = False
            return
        if cur.observe_only and cur.expected:
            self.phase = "check"
            self.step_checked = False
            return
        if not cur.instruction and not cur.expected:
            self._skip_empty()
            return
        self.phase = "do"
        self.step_checked = False
        self.step_ops = 0
        self.step_action_families = set()
        self.step_family_counts = {}
        self.step_intents_done = set()
        self.step_structural_caps_done = set()
        self.sms_auto_attempted = False
        self.step_goal_met = False
        self.step_effect_hit_streak = 0
        self.step_effect_hint = ""
        self.correction_hint = ""
        self.recovery_block_streak = 0
        self.require_do_work_streak = 0
        self.step_start_fp = ""
        self.do_subphase = "operation"
        self.step_nav_plan_hint = ""
        self.step_nav_plan_step_n = 0
        self.swipe_stuck_fp = ""
        self.swipe_stuck_dir = ""
        self.swipe_stuck_count = 0

    def note_swipe_pass(
        self,
        *,
        direction: str,
        pre_fp: str,
        post_fp: str,
    ) -> None:
        d = str(direction or "").strip().lower()
        pre = str(pre_fp or "").strip()
        post = str(post_fp or "").strip()
        if not d:
            return
        if pre and post and pre != post:
            self.swipe_stuck_fp = post
        else:
            self.swipe_stuck_fp = post or pre or self.swipe_stuck_fp
        if pre and post and pre != post:
            self.swipe_stuck_dir = d
            self.swipe_stuck_count = 0
            return
        if self.swipe_stuck_dir == d:
            self.swipe_stuck_count += 1
        else:
            self.swipe_stuck_dir = d
            self.swipe_stuck_count = 1

    def _skip_empty(self) -> str:
        if not self.advance():
            return "done"
        return self.phase

    def advance(self) -> bool:
        self.index += 1
        self.success_criteria_state = {}
        self.step_checked = False
        self.check_oracle_status = ""
        self.step_ops = 0
        self.step_action_families = set()
        self.step_family_counts = {}
        self.step_intents_done = set()
        self.step_structural_caps_done = set()
        self.sms_auto_attempted = False
        self.step_goal_met = False
        self.reset_guest_entry()
        self.step_effect_hit_streak = 0
        self.step_effect_hint = ""
        self.correction_hint = ""
        self.recovery_block_streak = 0
        self.require_do_work_streak = 0
        self.step_start_fp = ""
        self.do_subphase = "operation"
        self.step_nav_plan_hint = ""
        self.step_nav_plan_step_n = 0
        self.swipe_stuck_fp = ""
        self.swipe_stuck_dir = ""
        self.swipe_stuck_count = 0
        if self.index >= len(self.nodes):
            self.phase = "done"
            return False
        n = self.nodes[self.index].n if self.nodes else 0
        self.progress_gate.reset_milestone("do", n)
        self._sync()
        return True

    def enter_check(self) -> None:
        cur = self.current()
        if not cur:
            self.phase = "done"
            return
        if not cur.expected:
            self.advance()
            return
        self.phase = "check"
        self.step_checked = False
        self.check_oracle_status = ""
        self.check_session_refresh = True
        self.progress_gate.reset_milestone("check", cur.n if cur else 0)
        from mino_nexus.loop.vision_plan import reset_plan_context_for_check

        reset_plan_context_for_check(self)
        from mino_nexus.loop.milestones import on_enter_check_phase

        on_enter_check_phase(
            self,
            expected=str(cur.expected or ""),
            instruction=str(cur.instruction or ""),
        )

    def mark_checked(self) -> None:
        self.step_checked = True
        self.saw_assert = True

    def remember_tap(
        self,
        params: dict[str, Any],
        *,
        screen_fp: str = "",
        selector_text: str = "",
    ) -> None:
        try:
            self.last_tap = {
                "x": int(params.get("x")),
                "y": int(params.get("y")),
                "epoch": self.tap_epoch,
                "screen_fp": str(screen_fp or "").strip(),
                "selector_text": str(
                    selector_text or params.get("selector_text") or ""
                ).strip(),
            }
        except (TypeError, ValueError):
            return

    def clear_repeat_tap(self) -> None:
        """界面已导航离开（返回、滑动等）后，同坐标点击不算重复入口。"""
        self.tap_epoch += 1
        self.last_tap = None

    def prompt_block_full(self) -> str:
        """排查用：含已完成/未到步骤的完整指针（不注入 LLM）。"""
        return self._prompt_block_impl(scoped=False)

    def prompt_block(self) -> str:
        """注入 LLM：仅当前 phase / 当前用例步（P0）。"""
        return self._prompt_block_impl(scoped=True)

    def _prompt_block_impl(self, *, scoped: bool) -> str:
        if self.phase == "prep":
            lines = [
                _pt("prep_header"),
                f"[>] 前置（进行中）：{self.precondition}",
            ]
            if not scoped:
                for node in self.nodes:
                    lines.append(
                        _pt("step_future", n=node.n, instruction=node.instruction or "（无操作）")
                    )
            lines.append(_pt("prep_current", precondition=self.precondition))
            if self.otp_prep_hint:
                lines.append(self.otp_prep_hint)
            warn = str(getattr(self.progress_gate, "warning_hint", "") or "").strip()
            if warn:
                lines.append(warn)
            lines.append(_pt("prep_tail"))
            return "\n".join(lines)

        lines = [_pt("do_header")]
        if self.precondition and not scoped:
            lines.append(f"[x] 前置（已完成）：{self.precondition}")
        total = len(self.nodes)
        cur_idx = self.index
        cur = self.current()
        if scoped and cur:
            lines.append(_pt("step_unlock_hint", cur=cur.n, total=total))
        elif not scoped and total > 0:
            cur_n = self.nodes[cur_idx].n if 0 <= cur_idx < total else total
            lines.append(_pt("step_unlock_hint", cur=cur_n, total=total))
        if scoped and cur is None:
            lines.append(_pt("all_done"))
            return "\n".join(lines)
        if not scoped:
            for i, node in enumerate(self.nodes):
                if i > cur_idx:
                    continue
                if i < cur_idx:
                    bit = _pt("step_done", n=node.n, instruction=node.instruction or "（无操作）")
                    exp_done = str(node.expected or "").strip()
                    if exp_done:
                        bit += f" ｜ 预期：{exp_done}"
                    elif not self.all_uncheckable():
                        bit += " ｜ 本步无预期（已完成）"
                    lines.append(bit)
                    continue
                if self.phase == "do":
                    self.refresh_do_subphase()
                    if self.do_subphase == "achievement":
                        ach_line = (
                            _do_phase_achievement_brief(node.instruction, node.expected)
                            if _expected_defers_to_check(node.expected)
                            else _achievement_label(node.expected)
                        )
                    else:
                        ach_line = _pt("step_achievement_pending")
                    bit = _pt(
                        "step_active_do",
                        n=node.n,
                        instruction=node.instruction or "（无操作）",
                        achievement=ach_line,
                    )
                else:
                    bit = _pt(
                        "step_active_check",
                        n=node.n,
                        instruction=node.instruction or "（无操作）",
                        expected=node.expected,
                    )
                if not node.expected:
                    bit += " ｜ 本步无预期（做完即过）" if not self.all_uncheckable() else " ｜ 本步无预期（无法校验）"
                lines.append(bit)
        cur = self.current()
        if not cur:
            lines.append(_pt("all_done"))
            return "\n".join(lines)
        if self.phase == "do":
            self.refresh_do_subphase()
            ach = _achievement_label(cur.expected)
            ach_brief = _do_phase_achievement_brief(cur.instruction, cur.expected)
            if self.do_subphase == "achievement":
                ach_show = (
                    _do_phase_achievement_brief(cur.instruction, cur.expected)
                    if _expected_defers_to_check(cur.expected)
                    else ach
                )
                lines.append(
                    _pt(
                        "do_achievement",
                        n=cur.n,
                        achievement=ach_show,
                    )
                )
            else:
                lines.append(
                    _pt(
                        "do_operation",
                        n=cur.n,
                        instruction=cur.instruction,
                        achievement_brief=ach_brief,
                    )
                )
            if self.step_nav_plan_hint:
                lines.append(self.step_nav_plan_hint)
            if not str(cur.expected or "").strip():
                lines.append(_pt("do_no_expected"))
            if self.login_session_hint:
                lines.append(self.login_session_hint)
            guest_hint = compile_guest_entry_hint(
                cur.instruction,
                entry_tapped=self.guest_entry_tapped,
            )
            if guest_hint:
                lines.append(guest_hint)
            warn = str(getattr(self.progress_gate, "warning_hint", "") or "").strip()
            if warn:
                lines.append(warn)
            if self.step_effect_hint:
                lines.append(self.step_effect_hint)
            if self.correction_hint:
                lines.append(self.correction_hint)
            from mino_nexus.loop.step_intent import intent_micro_progress_line

            micro = intent_micro_progress_line(str(cur.instruction or ""))
            if not micro:
                from mino_nexus.loop.step_contract import instruction_micro_progress_line

                micro = instruction_micro_progress_line(str(cur.instruction or ""))
            if micro:
                lines.append(micro)
            from mino_nexus.loop.swipe_hint import instruction_swipe_direction, swipe_direction_label

            swipe_want = instruction_swipe_direction(str(cur.instruction or ""))
            if swipe_want:
                lab = swipe_direction_label(swipe_want)
                if lab:
                    lines.append(f"【滑动方向】用例要求 {lab}（勿与步骤原文相反）")
            from mino_nexus.loop.milestones import milestone_v1_enabled, milestones_brief_lines, read_state

            if milestone_v1_enabled():
                ms_lines = milestones_brief_lines(read_state(self), limit=8)
                if ms_lines:
                    lines.append("【子里程碑】")
                    lines.extend(ms_lines)
            tail_key = "do_tail_login_module" if self.login_module_prompt else "do_tail"
            lines.append(_pt(tail_key))
        else:
            lines.append(_pt("check_current", n=cur.n, expected=cur.expected or "（无预期，无法执行校验）"))
            from mino_nexus.loop.check_plan import build_check_plan, format_check_plan_brief

            plan = build_check_plan(str(cur.expected or ""), instruction=str(cur.instruction or ""))
            brief = format_check_plan_brief(plan)
            if brief:
                lines.append(brief)
            from mino_nexus.loop.milestones import milestone_v1_enabled, milestones_brief_lines, read_state

            if milestone_v1_enabled():
                ms_lines = milestones_brief_lines(read_state(self))
                if ms_lines:
                    lines.append("【子里程碑】")
                    lines.extend(ms_lines)
            lines.append(_pt("check_tail"))
        return "\n".join(lines)

    def decide_goal(self) -> str:
        if self.phase == "prep":
            return f"完成前置检查：{self.precondition}"
        cur = self.current()
        if not cur:
            return "完成本步操作"
        if self.phase == "check":
            return f"校验步骤 {cur.n} 的预期：{cur.expected}"
        return (cur.instruction or "").strip() or "完成本步操作"

    def decide_success_criteria_for_llm(self) -> str:
        from mino_nexus.loop.llm_step_context import success_criteria_json_text

        return success_criteria_json_text(self)

    def decide_success(self) -> str:
        if self.phase == "prep":
            return (
                f"前置已满足：{self.precondition}。"
                "不要去做步骤里的操作，不要校验预期。"
            )
        cur = self.current()
        if not cur:
            return "全部步骤已完成"
        if self.phase == "check":
            from mino_nexus.loop.milestones import milestone_v1_enabled

            if milestone_v1_enabled():
                return (
                    f"步骤 {cur.n} 的预期成立：{cur.expected}。"
                    "校验 success_criteria.milestones：状态由程序根据工具结果写回；"
                    "全部必填项通过后 signal_done。不要操作设备除非校验需要。"
                )
            return (
                f"步骤 {cur.n} 的预期成立：{cur.expected}。"
                "用 assert_visual 判断当前屏。不要操作设备。"
            )
        exp = str(cur.expected or "").strip()
        self.refresh_do_subphase()
        if exp and self.do_subphase == "achievement":
            return (
                f"完成步骤 {cur.n} 的操作：{cur.instruction}。"
                f"达成信号：{exp}。以达成信号为准，不以操作对象是否可见为准。"
            )
        if exp:
            return (
                f"完成步骤 {cur.n} 的操作：{cur.instruction}。"
                "先完成 instruction 中的全部子动作；达成信号将在子动作达标后展示。"
            )
        return (
            f"完成步骤 {cur.n} 的操作：{cur.instruction}。"
            "【本步无预期】做完操作即视为完成，不要试图找校验依据。"
        )
