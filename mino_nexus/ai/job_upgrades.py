"""llm_jobs 启动升级：在已有 Console 版本上追加程序侧 prompt 修订。"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

AGENT_DECIDE_V5_MARKER = "prompt_version >= 5（批跑 recovery / prep 收工）"
AGENT_DECIDE_V6_MARKER = "prompt_version >= 6（signal_skip / 迷路重启）"


def _patch_agent_decide_v5(text: str) -> str:
    """V4 → V5：prep 收工、done vs give_up、recovery 分流。"""
    out = str(text or "")

    prep_row = "| **前置** | 做完前置原文再 signal_done。不要进步骤、不要验预期。 |"
    prep_v5 = (
        "| **前置** | 做完前置原文再 signal_done。不要进步骤、不要验预期。"
        "**read_device_data 只读数据，不唤醒/解锁**（锁屏用 recover_screen_asleep_or_locked）。"
        "history 已有 sim=READY 等满足结果 → 必须 signal_done，禁止再 read_device_data。** |"
    )
    if prep_row in out:
        out = out.replace(prep_row, prep_v5, 1)

    do_row = (
        "| **操作** | 做完当前步骤后可直接 signal_done，或只调一次 tap/input（系统会自动进入校验）。不要跳步。 |"
    )
    do_v5 = (
        "| **操作** | 做完当前步骤后调 signal_done（目标已在屏上达成时也如此）。"
        "可只调一次 tap/input 后由系统进校验。不要跳步。"
        "**禁止用 signal_give_up 表示「本步已完成」。** |"
    )
    if do_row in out:
        out = out.replace(do_row, do_v5, 1)

    old_signals = """| 信号 | 含义 |
|------|------|
| signal_done | **当前阶段**完成，不是整条用例结束 |
| signal_give_up | 客观做不到（元素不存在、空态、路径不通、重试耗尽） |
| signal_ask_human | 需人提供可填入界面的信息（验证码、手机号等） |"""

    new_signals = """| 信号 | 含义 |
|------|------|
| signal_done | **当前阶段**完成（前置完成 / 本步操作完成 / 本步校验通过），进入下一阶段；**不是**整案结束 |
| signal_give_up | **客观无法继续**（元素不存在、空态、路径不通、重试用尽）；**禁止**在「本步其实已完成」时使用 |
| signal_ask_human | 需人提供可填入界面的信息（验证码、手机号等） |

**前置收工**：前置条件已在 history 或本次 tool 结果里确认满足 → 只调 `signal_done`（status=done），不要继续调 read_device_data 等检查类 prep 工具。

### 操作阶段：完成 vs 放弃（必守）

- 本步操作目标**已在当前屏达成**（例如步骤要求进验证码页，屏上已是验证码页）→ **`signal_done`**，进入本步校验。
- **`signal_give_up` 只用于**「按步骤做也达不到目标」或「设备/界面客观不允许继续」，**不是**「确认本步结束」的快捷键。
- 自检：thought 里出现「本步已完成 / 可以结束本步 / 进入校验 / 确认本步操作结束」→ 必须 `signal_done`，**禁止** `signal_give_up`。

### 前置工具边界（read_device_data）

- `read_device_data` **只负责读** SIM/设备属性（如 `sim=READY`），**不会、也不应**代替 `recover_screen_asleep_or_locked` 做唤醒/解锁。
- 若【已执行动作历史】里已有 `read_device_data → pass` 且 summary 含 `sim=READY`（或已满足前置原文），**下一动作必须是 `signal_done`**。
- 屏幕黑/锁屏：调 `recover_screen_asleep_or_locked`（或等开环 recovery），**不要**用 read_device_data 碰运气。
- 禁止「thought 写前置已完成，tool 仍选 read_device_data」—— thought 与 tool 必须一致。"""

    if old_signals in out:
        out = out.replace(old_signals, new_signals, 1)

    ui_tail = "内容态：骨架屏 / 转圈 → wait_ms；空态且本步要有数据 → give_up；折叠/抽屉未展开且本步要看隐藏内容 → 先展开再操作。\n"
    recovery_block = """内容态：骨架屏 / 转圈 → wait_ms；空态且本步要有数据 → give_up；折叠/抽屉未展开且本步要看隐藏内容 → 先展开再操作。

### 截图全黑时的分流

- 设备**未锁屏**但截图全黑（常见于 FLAG_SECURE、密码框）：**不要**重复 `recover_screen_asleep_or_locked`；改用 `recover_screen_secure_or_no_capture` 的提示，或依赖 hierarchy / signal_ask_human。
- 若 history 多次 wake 仍黑且屏上并非锁屏 → 按禁止截屏处理，禁止机械重复 wake。

### 前台不是被测 App

- 本步要在被测 App 内操作，但屏上是微信/系统设置等：`recover_bring_target_app_foreground` 或 `launch_app`（目标包），再继续。
- 步骤原文明确要求跳转第三方（如微信 OAuth）时除外；OAuth 结束回到业务步骤前再拉回被测 App。

"""
    if ui_tail in out and "### 截图全黑时的分流" not in out:
        out = out.replace(ui_tail, recovery_block, 1)

    if AGENT_DECIDE_V5_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {AGENT_DECIDE_V5_MARKER} -->\n"

    return out


def _patch_agent_decide_v6(text: str) -> str:
    """V5 → V6：signal_skip + 迷路时重启 App。"""
    out = str(text or "")

    old_skip_row = "| signal_ask_human | 需人提供可填入界面的信息（验证码、手机号等） |"
    new_skip_rows = """| signal_ask_human | 需人提供可填入界面的信息（验证码、手机号等） |
| signal_skip | **整案跳过**（渠道/设备不对、缺专属入口、客观无法在本机执行）；必须写清 `reason`，不要用 give_up 代替 |"""

    if old_skip_row in out and "signal_skip" not in out:
        out = out.replace(old_skip_row, new_skip_rows, 1)

    skip_block = """### signal_skip（渠道 / 客观不可执行）

- 用例要求 **iOS 专属能力**（Apple ID、Face ID 等）而当前是 Android/Web → `signal_skip` + reason，不要 give_up/ask_human 硬跑。
- 用例 `platform` 与当前设备渠道明显不符（程序也可能已自动 skip）→ 同样用 `signal_skip` 说明。
- **不是**「本步做不到」：单步做不到用 `signal_give_up`；整案不该在这台设备/这个渠道跑才用 `signal_skip`。

### 迷路 / 回不到起点（批跑常见）

- 上一条用例留下深层页面栈，本步要在**登录页/首页**找入口却找不到 → **不要** `launch_app` / `BACK` / 同坐标 tap 无限循环。
- 恢复顺序：① `logout` / 返回到业务起点 ② 仍不对 → **`recover_restart_target_app`**（关进程再启动目标包，相当于冷启动）③ 再执行本步。
- 登录模块用例开环可能已做过 cold start；迷路后仍可再调 `recover_restart_target_app`，然后重新找入口。
- 重启后先 `observe` 确认页面，再继续；不要用 `signal_give_up` 代替重启。

### 输入法 / 截图全黑（IME）

- 键盘弹出时部分界面 `capture_black=yes`（安全键盘 / FLAG_SECURE）：先 `press_key` BACK 收起键盘，或 `wait_ms` 后再截图。
- 若多次黑图且 `ime_shown=yes`，优先关键盘再继续；**不要**反复 wake。
- 实验设备可在 Scout 侧切换 **ADB Keyboard**（`com.android.adbkeyboard`）减少系统输入法挡截图（需 Scout 实现 `set_input_method`，Nexus 不直连 adb）。

"""
    anchor = "### 前台不是被测 App"
    if anchor in out and "### 迷路 / 回不到起点" not in out:
        out = out.replace(anchor, skip_block + anchor, 1)

    if AGENT_DECIDE_V6_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {AGENT_DECIDE_V6_MARKER} -->\n"
    return out


def upgrade_agent_decide_to_v5() -> int:
    """已有 agent-decide 且 prompt_version < 5 时，基于当前正文打 V5 补丁。"""
    from mino_nexus.services.job_store import _blocks_snapshot, _revision_list, _set_revisions, _smoke_render, _validate_job, get_job

    row = get_job("agent-decide")
    if not row:
        return 0
    ver = int(row.get("prompt_version") or 1)
    if ver >= 5:
        return 0

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v5(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks

    revisions = _revision_list(row)
    revisions.append({
        "version": ver,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": f"v{ver} before program upgrade to v5",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 5
    _set_revisions(merged, revisions)

    _validate_job(merged)
    _smoke_render(merged)

    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob
    from mino_nexus.services.job_store import _to_row

    with session_scope() as db:
        db_row = db.query(LlmJob).filter(LlmJob.id == "agent-decide").first()
        if db_row is None:
            db.add(_to_row({**merged, "id": "agent-decide", "builtin": True}))
        else:
            db_row.system_blocks_json = list(merged.get("system_blocks") or [])
            db_row.user_blocks_json = list(merged.get("user_blocks") or [])
            db_row.prompt_version = 5
            db_row.overrides_json = dict(merged.get("overrides_json") or {})
        db.flush()
    return 1


def upgrade_agent_decide_to_v6() -> int:
    """已有 agent-decide 且 prompt_version < 6 时，基于当前正文打 V6 补丁。"""
    from mino_nexus.services.job_store import _blocks_snapshot, _revision_list, _set_revisions, _smoke_render, _validate_job, get_job

    row = get_job("agent-decide")
    if not row:
        return 0
    ver = int(row.get("prompt_version") or 1)
    if ver >= 6:
        return 0
    if ver < 5:
        upgrade_agent_decide_to_v5()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v6(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks

    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 5),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": f"v{row.get('prompt_version')} before program upgrade to v6",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 6
    _set_revisions(merged, revisions)

    _validate_job(merged)
    _smoke_render(merged)

    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob
    from mino_nexus.services.job_store import _to_row

    with session_scope() as db:
        db_row = db.query(LlmJob).filter(LlmJob.id == "agent-decide").first()
        if db_row is None:
            db.add(_to_row({**merged, "id": "agent-decide", "builtin": True}))
        else:
            db_row.system_blocks_json = list(merged.get("system_blocks") or [])
            db_row.user_blocks_json = list(merged.get("user_blocks") or [])
            db_row.prompt_version = 6
            db_row.overrides_json = dict(merged.get("overrides_json") or {})
        db.flush()
    return 1
