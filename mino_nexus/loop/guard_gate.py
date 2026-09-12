"""GuardGate：导航守卫的分级干预。与 ProgressGate **并行独立计数**（设计稿 §8.2）。

阶梯（同一 `guard_id`）：

| 次数 | 行为 |
|---|---|
| 1 | steer —— 注入【禁止】+ history，**不计** `no_progress_streak` |
| 2 | block —— skip 掉这个 cap |
| 3 | stop —— `run_type` 有人值守则 ask_human，无人值守则 give_up |

强度门槛（§8.2 硬约束、§8.3）：

| strength | 能否进 block/stop |
|---|---|
| `strong_id` | 能，直接走阶梯 |
| `strong_text` | 能，但要先过**连续命中门槛**：命中 +1，未命中 / 无 hierarchy **归零** |
| `medium` | **不能**，只用于推荐 |
| `weak` | **不能**，只 steer |

未命中 detect → 只 steer + Wiki，不 hard block —— 否则 `guard_fp` 必然超标。
连续命中计数是运行时状态，**不写 DB**（§8.3 末句）。

本文件只读 `nav_fsm_store` 加载进来的 guards，**不硬编码任何 App 文案**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from mino_nexus.services.nav_fsm import (
    STRENGTH_STRONG_TEXT,
    GuardHit,
)

ACTION_ALLOW = "allow"
ACTION_STEER = "steer"
ACTION_BLOCK = "block"
ACTION_STOP = "stop"

# 有人值守 → 问人；无人值守 → 放弃（§11.5）
_ATTENDED_RUN_TYPES = frozenset({"manual", "copilot"})

# 只有点击类 cap 会踩 tap_forbidden。其它 cap 不进 guard 判定，免得误伤。
_TAP_CAPS = frozenset({"tap_element", "multi_tap", "long_press_element"})

# 允许集之外的**点击**，用这个合成 guard_id 计数（只管点击，理由见 check() 里的注释）
CAP_NOT_ALLOWED = "nav.cap_not_allowed"


@dataclass
class Verdict:
    action: str = ACTION_ALLOW
    guard_id: str = ""
    strength: str = ""
    reason: str = ""
    wiki_ref: str = ""
    ladder: int = 0
    consecutive_hits: int = 0
    stop_signal: str = ""  # ask_human | give_up

    def blocking(self) -> bool:
        return self.action in (ACTION_BLOCK, ACTION_STOP)


@dataclass
class GuardGate:
    """一条用例一个实例。计数按 `guard_id` 各自独立。"""

    run_type: str = "manual"
    _ladder: dict[str, int] = field(default_factory=dict)
    _consecutive: dict[str, int] = field(default_factory=dict)

    # ---------------- 每 turn 先刷连续命中 ----------------

    def observe(self, hits: list[GuardHit], *, hierarchy_ok: bool) -> None:
        """更新 `strong_text` 的连续命中计数。**必须每 turn 调一次**，包括拿不到 hierarchy 的 turn。

        没有 hierarchy 就等于「未命中」→ 全部归零。断连归零是 §8.3 明写的规则：
        不归零的话，隔了十轮的两次命中会被当成连续两次，凭空升到 block。
        """
        if not hierarchy_ok:
            self._consecutive.clear()
            return
        for hit in hits or []:
            if hit.strength != STRENGTH_STRONG_TEXT:
                continue
            if hit.detected:
                self._consecutive[hit.guard_id] = self._consecutive.get(hit.guard_id, 0) + 1
            else:
                self._consecutive.pop(hit.guard_id, None)

    def consecutive_hits(self, guard_id: str) -> int:
        return int(self._consecutive.get(str(guard_id), 0))

    # ---------------- 判本步动作 ----------------

    def check(
        self,
        *,
        cap_id: str,
        params: dict[str, Any] | None,
        hits: list[GuardHit],
        hierarchy_ok: bool,
        allowed: list[str] | None = None,
        enforce_allowed: bool = False,
    ) -> Verdict:
        """本步动作要不要拦。`enforce_allowed` 只在高置信档打开（见下方注释）。"""
        if not hierarchy_ok:
            # 无输入不误判（§10.0.2）：拿不到层级这一轮，GuardGate 一律放行
            return Verdict()

        hit, item = self._violated(cap_id, params, hits)
        if hit is not None and item is not None:
            return self._ladder_verdict(
                hit.guard_id,
                strength=hit.strength,
                reason=str(item.get("reason") or f"{hit.widget}={hit.widget_state} 时禁止该操作"),
                wiki_ref=hit.wiki_ref,
                blockable=hit.blockable(),
            )

        # 模型点了本该拦的东西，但 detect 没命中 —— 记候选，交人工标 fn（§9），不拦
        miss = self._miss_candidate(cap_id, params, hits)
        if miss is not None:
            return Verdict(
                action=ACTION_ALLOW,
                guard_id=miss.guard_id,
                strength=miss.strength,
                reason="detect 未命中，记 guard_miss_candidate 交人工判定",
            )

        # 允许集**只对点击类 cap 生效**。框架自己会注入 assert_visual、恢复边、signal_*，
        # 它们不可能出现在导航图配的允许集里 —— 一并硬判会把脚本校验和恢复路径全拦死，
        # 那是比漏拦严重得多的故障。真正需要约束的偏离是「乱点别的元素」。
        if (
            enforce_allowed
            and allowed
            and str(cap_id or "") in _TAP_CAPS
            and str(cap_id or "") not in set(allowed)
        ):
            return self._ladder_verdict(
                CAP_NOT_ALLOWED,
                strength="",
                reason=f"{cap_id} 不在本步允许动作集内",
                wiki_ref="",
                blockable=True,
            )
        return Verdict()

    # ---------------- 内部 ----------------

    def _violated(
        self, cap_id: str, params: dict[str, Any] | None, hits: list[GuardHit]
    ) -> tuple[Optional[GuardHit], Optional[dict[str, Any]]]:
        if str(cap_id or "") not in _TAP_CAPS:
            return None, None
        for hit in hits or []:
            if not hit.detected:
                continue
            for item in hit.tap_forbidden:
                if _params_match(params, item.get("match")):
                    return hit, item
        return None, None

    def _miss_candidate(
        self, cap_id: str, params: dict[str, Any] | None, hits: list[GuardHit]
    ) -> Optional[GuardHit]:
        if str(cap_id or "") not in _TAP_CAPS:
            return None
        for hit in hits or []:
            if hit.detected:
                continue
            for item in hit.tap_forbidden:
                if _params_match(params, item.get("match")):
                    return hit
        return None

    def _ladder_verdict(
        self,
        guard_id: str,
        *,
        strength: str,
        reason: str,
        wiki_ref: str,
        blockable: bool,
    ) -> Verdict:
        gid = str(guard_id or "")
        step = self._ladder.get(gid, 0) + 1
        self._ladder[gid] = step
        consecutive = self.consecutive_hits(gid)

        if strength == STRENGTH_STRONG_TEXT:
            # strong_text 的阶梯位 = 连续命中数（§8.3），比 strong_id 多待一轮
            step = min(step, max(consecutive, 1))

        action = ACTION_STEER
        if blockable:
            if step >= 3:
                action = ACTION_STOP
            elif step >= 2:
                action = ACTION_BLOCK

        return Verdict(
            action=action,
            guard_id=gid,
            strength=strength,
            reason=reason,
            wiki_ref=wiki_ref,
            ladder=step,
            consecutive_hits=consecutive,
            stop_signal=self.stop_signal() if action == ACTION_STOP else "",
        )

    def stop_signal(self) -> str:
        return "ask_human" if str(self.run_type or "manual").lower() in _ATTENDED_RUN_TYPES else "give_up"


def _params_match(params: dict[str, Any] | None, match: Any) -> bool:
    """`tap_forbidden[].match` 与本步 params 是否吻合。

    文案用**包含**而非全等：模型常给「关注 」「关注按钮」这类带饰词的 selector_text，
    要求全等等于白配。
    """
    if not isinstance(match, dict) or not match:
        return False
    p = dict(params or {})
    for key, want in match.items():
        got = p.get(key)
        if got is None:
            return False
        if isinstance(want, str):
            if str(want) not in str(got):
                return False
        elif got != want:
            return False
    return True
