"""守卫恢复方案。落在扩展包基座行，供看图规划读下一回合该派什么。"""
from __future__ import annotations

# 留下的守卫。next_cap 为空表示按当前 in_progress 里程碑继续，不要再派 avoid。
GUARD_RECOVERY: dict[str, dict[str, str]] = {
    "prep_clear_before_launch": {
        "phases": "prep",
        "summary": "声明了清缓存时，先 clear_app_cache，不要 launch_app。",
        "next_cap": "clear_app_cache",
        "avoid": "launch_app",
    },
    "exec_script_params": {
        "phases": "prep",
        "summary": "脚本参数不齐。补全参数后再派，不要空参执行。",
        "next_cap": "exec_script",
        "avoid": "",
    },
    "action_fuse": {
        "phases": "prep,do",
        "summary": "同一动作没有进展。换目标或 signal_give_up，不要再派同一个点和同一个 cap。",
        "next_cap": "",
        "avoid": "",
    },
    "limit_recovery_retry": {
        "phases": "do",
        "summary": "恢复次数已用尽。回到当前 in_progress 的业务步骤，不要再派同类恢复。",
        "next_cap": "",
        "avoid": "recover_",
    },
    "require_sms_send_before_otp": {
        "phases": "do",
        "summary": "还没发码。先点发送验证码，不要 get_otp，也不要填验证码。",
        "next_cap": "tap_element",
        "avoid": "get_otp,input_text",
    },
    "require_otp_before_login_tap": {
        "phases": "do",
        "summary": "验证码还没填。先 input_text 填验证码，不要点登录。",
        "next_cap": "input_text",
        "avoid": "tap_element",
    },
    "block_repeat_get_otp_when_ready": {
        "phases": "do",
        "summary": "验证码已经取到。input_text 填入，不要再 get_otp。",
        "next_cap": "input_text",
        "avoid": "get_otp",
    },
    "block_fsm_off_step_target": {
        "phases": "do",
        "summary": "导航目标不是本步要去的页。fsm_navigate 只去本步目标。",
        "next_cap": "fsm_navigate",
        "avoid": "",
    },
    "block_back_without_nav_back_semantics": {
        "phases": "do",
        "summary": "这一步没有返回语义。不要 press_key 返回，继续当前 in_progress。",
        "next_cap": "",
        "avoid": "press_key",
    },
    "block_assert_in_do": {
        "phases": "do",
        "summary": "断言只在校验阶段。操作阶段把当前里程碑做完，不要 assert_visual。",
        "next_cap": "",
        "avoid": "assert_visual",
    },
    "deny_mutate": {
        "phases": "check",
        "summary": "校验阶段不要点击或输入。只做 assert_visual。",
        "next_cap": "assert_visual",
        "avoid": "tap_element,input_text",
    },
    "force_case_expectation": {
        "phases": "check",
        "summary": "断言文案用本步用例预期，不要改写预期。",
        "next_cap": "assert_visual",
        "avoid": "",
    },
}


def recovery_text(guard_id: str, reason: str = "") -> str:
    row = GUARD_RECOVERY.get(str(guard_id or "").strip()) or {}
    summary = str(row.get("summary") or "").strip()
    nxt = str(row.get("next_cap") or "").strip()
    avoid = str(row.get("avoid") or "").strip()
    bits = [summary]
    if nxt:
        bits.append(f"下一步派 {nxt}。")
    elif summary:
        bits.append("下一步派当前 in_progress 里程碑上的能力。")
    if avoid:
        bits.append(f"不要再派 {avoid}。")
    if reason:
        bits.append(str(reason).strip())
    return " ".join(b for b in bits if b)[:500]


def recovery_for_guard(guard_id: str) -> dict[str, str]:
    gid = str(guard_id or "").strip()
    row = dict(GUARD_RECOVERY.get(gid) or {})
    if not row:
        return {}
    try:
        from mino_nexus.core.database import session_scope
        from mino_nexus.models.catalog import CatalogEntry

        with session_scope() as db:
            ent = (
                db.query(CatalogEntry)
                .filter(CatalogEntry.kind == "base", CatalogEntry.id == f"guard.{gid}")
                .first()
            )
            payload = dict(ent.payload_json or {}) if ent is not None else {}
        stored = payload.get("recovery") if isinstance(payload.get("recovery"), dict) else {}
        if stored:
            row.update({k: str(v) for k, v in stored.items() if v})
    except Exception:  # noqa: BLE001
        pass
    return row


def seed_guard_recovery_rows() -> int:
    """基座行：id = guard.<守卫 id>。已存在则不覆盖控制台改过的 recovery。"""
    from mino_nexus.catalog.writer import upsert
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.catalog import CatalogEntry

    n = 0
    with session_scope() as db:
        existing = {
            str(row.id)
            for row in db.query(CatalogEntry).filter(CatalogEntry.kind == "base").all()
        }
    for gid, spec in GUARD_RECOVERY.items():
        eid = f"guard.{gid}"
        if eid in existing:
            continue
        upsert(
            "base",
            eid,
            {
                "display_name": gid,
                "description": spec["summary"],
                "visible_to": ["system"],
                "platforms": ["android", "ios", "web"],
                "category": "guard",
                "payload": {
                    "guard_id": gid,
                    "when": spec["summary"],
                    "recovery": {
                        "summary": spec["summary"],
                        "next_cap": spec["next_cap"],
                        "avoid": spec["avoid"],
                        "phases": spec["phases"],
                    },
                },
            },
        )
        n += 1
    return n
