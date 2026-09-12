"""NavFSM 遥测。写 `session_log`，字段名**定型后不再改名**（设计稿 §10.6）。

`session_harness` 与 §9 的指标只读这里定下的 key；新增字段一律 optional，
不动已有 key —— 否则历史 session 的 CSV 导出会碎。

四个事件：
  nav/localize               每 turn localize 后
  nav/edge_attempt           边执行后 / effect_assert 评估后
  nav/guard_hit              guard 触发 steer / block / stop
  nav/guard_miss_candidate   模型点了本该拦的 cap，但 detect 没命中（人工标 fn 用）
"""
from __future__ import annotations

from typing import Any

EVENT_LOCALIZE = "nav/localize"
EVENT_EDGE_ATTEMPT = "nav/edge_attempt"
EVENT_GUARD_HIT = "nav/guard_hit"
EVENT_GUARD_MISS = "nav/guard_miss_candidate"

# §10.6 的完整 key 集。值可空，key 不可少 —— 下游按固定列读。
_KEYS = (
    "turn_id",
    "run_id",
    "case_id",
    "state_id",
    "confidence",
    "edge_id",
    "guard_id",
    "strength",
    "action",
    "strong_text_consecutive_hits",
    "cap_id",
    "result",
    "hierarchy_stale",
    "observe_hierarchy",
)


def _emit(event: str, payload: dict[str, Any]) -> None:
    """写不进去也不能影响跑批 —— 遥测是观测，不是主链。"""
    try:
        from mino_nexus.loop.session_log import active_writer

        writer = active_writer()
        if writer is None:
            return
        row = {k: payload.get(k) for k in _KEYS}
        row.update({k: v for k, v in payload.items() if k not in _KEYS})
        writer.append(event, row)
    except Exception:  # noqa: BLE001
        pass


def localize(**payload: Any) -> None:
    _emit(EVENT_LOCALIZE, payload)


def edge_attempt(**payload: Any) -> None:
    _emit(EVENT_EDGE_ATTEMPT, payload)


def guard_hit(**payload: Any) -> None:
    _emit(EVENT_GUARD_HIT, payload)


def vlm_widget_state(**payload: Any) -> None:
    """VLM 兜底判 widget 态（§2.2）。optional 字段，不影响 §10.6 最小 key 集。"""
    _emit("nav/vlm_widget_state", payload)


def guard_miss_candidate(**payload: Any) -> None:
    """QA 在 Console 标 `confirmed_fn: true/false`，再算 guard_fn（§9）。

    首期**不做自动判定** —— 漏拦本来就得人看，自动判会把噪声当指标。
    """
    _emit(EVENT_GUARD_MISS, payload)


# ---------------- 人工标注与指标聚合（设计稿 §9） ----------------
#
# §9 明说 `guard_fn`「首期**不做自动判定**」：漏拦本来就得人看，自动判会把噪声当指标。
# 所以这里只提供：① 让 QA 把判断写回 session log；② 按标注算 fp / fn。
# 没人标注时指标是 `None` 而不是 0 —— 「没测过」和「没问题」不是一回事。

ANNOTATION_EVENT = "nav/annotation"

# 对 guard_hit（拦了）的判断
VERDICT_TRUE_POSITIVE = "true_positive"
VERDICT_FALSE_POSITIVE = "false_positive"
# 对 guard_miss_candidate（没拦）的判断
VERDICT_CONFIRMED_FN = "confirmed_fn"
VERDICT_NOT_FN = "not_fn"

VERDICTS = frozenset(
    {VERDICT_TRUE_POSITIVE, VERDICT_FALSE_POSITIVE, VERDICT_CONFIRMED_FN, VERDICT_NOT_FN}
)

_BLOCKING_ACTIONS = frozenset({"block", "stop"})


class AnnotationError(ValueError):
    """标注对象不对（seq 不存在 / 事件类型不可标 / verdict 非法）。"""


def annotate(
    session_id: str,
    *,
    target_seq: int,
    verdict: str,
    note: str = "",
    by: str = "",
) -> dict[str, Any]:
    """QA 把判断写回 session log。

    **不改原事件** —— session log 是 append-only，标注是新的一行，指向被标的 `seq`。
    同一个 seq 可以标多次，聚合时取最后一条（改主意了以后一条为准）。
    """
    from mino_nexus.services import session_store

    sid = str(session_id or "").strip()
    v = str(verdict or "").strip()
    if v not in VERDICTS:
        raise AnnotationError(f"verdict 只能是 {sorted(VERDICTS)} 之一，收到 {verdict!r}")

    target = _event_at(sid, int(target_seq))
    if target is None:
        raise AnnotationError(f"session={sid} 里没有 seq={target_seq}")
    ttype = str(target.get("type") or "")
    if ttype == EVENT_GUARD_HIT and v not in (VERDICT_TRUE_POSITIVE, VERDICT_FALSE_POSITIVE):
        raise AnnotationError(f"{EVENT_GUARD_HIT} 只能标 true_positive / false_positive")
    if ttype == EVENT_GUARD_MISS and v not in (VERDICT_CONFIRMED_FN, VERDICT_NOT_FN):
        raise AnnotationError(f"{EVENT_GUARD_MISS} 只能标 confirmed_fn / not_fn")
    if ttype not in (EVENT_GUARD_HIT, EVENT_GUARD_MISS):
        raise AnnotationError(f"seq={target_seq} 是 {ttype}，不是可标注的 guard 事件")

    payload = {
        "target_seq": int(target_seq),
        "target_event": ttype,
        "verdict": v,
        "note": str(note or "")[:600],
        "by": str(by or ""),
        "guard_id": str((target.get("payload") or {}).get("guard_id") or ""),
    }
    session_store.append_event(
        session_id=sid,
        type=ANNOTATION_EVENT,
        payload=payload,
        turn=int(target.get("turn") or 0),
    )
    return payload


def _event_at(session_id: str, seq: int) -> dict[str, Any] | None:
    from mino_nexus.services import session_store

    rows = session_store.read_events(session_id, from_seq=int(seq), limit=1)
    if not rows or int(rows[0].get("seq") or -1) != int(seq):
        return None
    return rows[0]


def _all_events(session_id: str) -> list[dict[str, Any]]:
    from mino_nexus.services import session_store

    out: list[dict[str, Any]] = []
    cursor = 0
    while True:
        page = session_store.read_events(session_id, from_seq=cursor, limit=2000)
        if not page:
            break
        out.extend(page)
        cursor = int(page[-1].get("seq") or 0) + 1
        if len(page) < 2000:
            break
    return out


def _ratio(hit: int, total: int) -> float | None:
    """没有分母就回 None。0/0 报成 0 会让「没跑过」看起来像「全对」。"""
    return round(hit / total, 4) if total else None


def aggregate_session(session_id: str, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """单条 session 的 nav 指标。字段名与 §9 的指标表对齐。"""
    rows = events if events is not None else _all_events(session_id)

    localize = [e for e in rows if e.get("type") == EVENT_LOCALIZE]
    edges = [e for e in rows if e.get("type") == EVENT_EDGE_ATTEMPT]
    guard_hits = [e for e in rows if e.get("type") == EVENT_GUARD_HIT]
    guard_miss = [e for e in rows if e.get("type") == EVENT_GUARD_MISS]

    # 标注取每个 target_seq 的最后一条
    verdicts: dict[int, str] = {}
    for e in rows:
        if e.get("type") != ANNOTATION_EVENT:
            continue
        payload = e.get("payload") or {}
        verdicts[int(payload.get("target_seq") or -1)] = str(payload.get("verdict") or "")

    localized = [e for e in localize if str((e.get("payload") or {}).get("state_id") or "")]
    edge_pass = [e for e in edges if str((e.get("payload") or {}).get("result") or "") == "pass"]
    edge_fail = [e for e in edges if str((e.get("payload") or {}).get("result") or "") == "fail"]
    blocking = [
        e for e in guard_hits
        if str((e.get("payload") or {}).get("action") or "") in _BLOCKING_ACTIONS
    ]

    fp_rows = [e for e in blocking if int(e.get("seq") or -1) in verdicts]
    fp_bad = [e for e in fp_rows if verdicts[int(e["seq"])] == VERDICT_FALSE_POSITIVE]
    fn_rows = [e for e in guard_miss if int(e.get("seq") or -1) in verdicts]
    fn_bad = [e for e in fn_rows if verdicts[int(e["seq"])] == VERDICT_CONFIRMED_FN]

    by_state: dict[str, int] = {}
    for e in localized:
        sid = str((e.get("payload") or {}).get("state_id") or "")
        by_state[sid] = by_state.get(sid, 0) + 1

    by_guard: dict[str, dict[str, int]] = {}
    for e in guard_hits:
        payload = e.get("payload") or {}
        gid = str(payload.get("guard_id") or "")
        bucket = by_guard.setdefault(gid, {"steer": 0, "block": 0, "stop": 0})
        action = str(payload.get("action") or "")
        if action in bucket:
            bucket[action] += 1

    return {
        "session_id": str(session_id or ""),
        "localize_turns": len(localize),
        "localize_hit": len(localized),
        # 注意：这是「判出了某个 state」的比例，**不是** §9 要的 localize_acc。
        # 后者是「判得对不对」，只能人工抽检 —— 见 localize_samples。
        "localize_resolved_rate": _ratio(len(localized), len(localize)),
        "localize_states": by_state,
        "edge_attempts": len(edges),
        "edge_success_rate": _ratio(len(edge_pass), len(edge_pass) + len(edge_fail)),
        "edge_pass": len(edge_pass),
        "edge_fail": len(edge_fail),
        "guard_hits": len(guard_hits),
        "guard_blocking": len(blocking),
        "guard_by_id": by_guard,
        "guard_fp": _ratio(len(fp_bad), len(fp_rows)),
        "guard_fp_reviewed": len(fp_rows),
        "guard_fp_pending": len(blocking) - len(fp_rows),
        "guard_fn": _ratio(len(fn_bad), len(fn_rows)),
        "guard_fn_reviewed": len(fn_rows),
        "guard_fn_pending": len(guard_miss) - len(fn_rows),
        "guard_miss_candidates": len(guard_miss),
        "vlm_invoked": sum(
            1 for e in localize if (e.get("payload") or {}).get("vlm_invoked")
        ),
        "vlm_invoked_rate": _ratio(
            sum(1 for e in localize if (e.get("payload") or {}).get("vlm_invoked")), len(localize)
        ),
        "hierarchy_stale_turns": sum(
            1 for e in localize if (e.get("payload") or {}).get("hierarchy_stale")
        ),
    }


def localize_samples(session_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """给人工抽检 localize 准确率用的样本（§9：≥90% 骨干屏，靠人抽检）。"""
    rows = [e for e in _all_events(session_id) if e.get("type") == EVENT_LOCALIZE]
    out = []
    for e in rows[: max(1, int(limit))]:
        payload = e.get("payload") or {}
        out.append(
            {
                "seq": int(e.get("seq") or 0),
                "turn_id": payload.get("turn_id"),
                "state_id": payload.get("state_id"),
                "confidence": payload.get("confidence"),
                "band": payload.get("band"),
                "ambiguous": payload.get("ambiguous"),
                "degraded": payload.get("degraded"),
            }
        )
    return out


def pending_reviews(session_id: str) -> list[dict[str, Any]]:
    """还没人标的 guard 事件。Console 的待办列表。"""
    rows = _all_events(session_id)
    done = {
        int((e.get("payload") or {}).get("target_seq") or -1)
        for e in rows
        if e.get("type") == ANNOTATION_EVENT
    }
    out = []
    for e in rows:
        etype = str(e.get("type") or "")
        payload = e.get("payload") or {}
        if etype == EVENT_GUARD_HIT and str(payload.get("action") or "") not in _BLOCKING_ACTIONS:
            continue  # steer 不算拦，不进 fp 抽检
        if etype not in (EVENT_GUARD_HIT, EVENT_GUARD_MISS):
            continue
        seq = int(e.get("seq") or -1)
        if seq in done:
            continue
        out.append(
            {
                "session_id": str(session_id or ""),
                "seq": seq,
                "event": etype,
                "turn_id": payload.get("turn_id"),
                "guard_id": payload.get("guard_id"),
                "strength": payload.get("strength"),
                "action": payload.get("action"),
                "cap_id": payload.get("cap_id"),
                "state_id": payload.get("state_id"),
                "verdict_options": (
                    [VERDICT_TRUE_POSITIVE, VERDICT_FALSE_POSITIVE]
                    if etype == EVENT_GUARD_HIT
                    else [VERDICT_CONFIRMED_FN, VERDICT_NOT_FN]
                ),
            }
        )
    return out


def aggregate_app(app_id: str, *, run_id: str = "", limit: int = 50) -> dict[str, Any]:
    """跨 session 汇总。Console 看板与 `mino-nexus nav metrics` 共用这一份。"""
    from mino_nexus.services import session_store

    sessions, _total = session_store.list_sessions(
        app_id=str(app_id or ""), run_id=str(run_id or ""), limit=max(1, min(200, int(limit)))
    )
    per: list[dict[str, Any]] = []
    totals = {
        "localize_turns": 0, "localize_hit": 0,
        "edge_pass": 0, "edge_fail": 0,
        "guard_hits": 0, "guard_blocking": 0, "guard_miss_candidates": 0,
        "guard_fp_reviewed": 0, "guard_fp_bad": 0,
        "guard_fn_reviewed": 0, "guard_fn_bad": 0,
        "vlm_invoked": 0,
    }
    for row in sessions:
        sid = str(row.get("session_id") or "")
        if not sid:
            continue
        events = _all_events(sid)
        if not any(str(e.get("type") or "").startswith("nav/") for e in events):
            continue  # 这条 session 压根没跑 NavFSM，别拿进分母
        m = aggregate_session(sid, events=events)
        per.append(m)
        for key in ("localize_turns", "localize_hit", "edge_pass", "edge_fail",
                    "guard_hits", "guard_blocking", "guard_miss_candidates",
                    "guard_fp_reviewed", "guard_fn_reviewed", "vlm_invoked"):
            totals[key] += int(m.get(key) or 0)
        if m.get("guard_fp") is not None:
            totals["guard_fp_bad"] += round(float(m["guard_fp"]) * int(m["guard_fp_reviewed"]))
        if m.get("guard_fn") is not None:
            totals["guard_fn_bad"] += round(float(m["guard_fn"]) * int(m["guard_fn_reviewed"]))

    return {
        "app_id": str(app_id or ""),
        "run_id": str(run_id or ""),
        "sessions": len(per),
        "localize_resolved_rate": _ratio(totals["localize_hit"], totals["localize_turns"]),
        "edge_success_rate": _ratio(totals["edge_pass"], totals["edge_pass"] + totals["edge_fail"]),
        "guard_fp": _ratio(totals["guard_fp_bad"], totals["guard_fp_reviewed"]),
        "guard_fn": _ratio(totals["guard_fn_bad"], totals["guard_fn_reviewed"]),
        "vlm_invoked_rate": _ratio(totals["vlm_invoked"], totals["localize_turns"]),
        "totals": totals,
        "targets": {
            # §9 的建议目标，放进响应里省得看板再抄一遍
            "localize_acc": ">= 0.90（人工抽检骨干屏）",
            "edge_success_rate": ">= 0.85（关键边）",
            "guard_fp": "< 0.10",
            "guard_fn": "< 0.05",
        },
        "per_session": per,
    }
