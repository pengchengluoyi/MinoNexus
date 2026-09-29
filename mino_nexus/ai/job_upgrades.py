"""llm_jobs 启动升级：在已有 Console 版本上追加程序侧 prompt 修订。"""
from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any

AGENT_DECIDE_V5_MARKER = "prompt_version >= 5（批跑 recovery / prep 收工）"
AGENT_DECIDE_V6_MARKER = "prompt_version >= 6（signal_skip / 迷路重启）"
AGENT_DECIDE_V7_MARKER = "prompt_version >= 7（NavFSM RouteAssist / 导航守卫）"
AGENT_DECIDE_V8_MARKER = "prompt_version >= 8（screen_layout 布局线框）"
AGENT_DECIDE_V9_MARKER = "prompt_version >= 9（文档库 doc_context 摘录）"
AGENT_DECIDE_V10_MARKER = "prompt_version >= 10（vlm_hierarchy）"
AGENT_DECIDE_V11_MARKER = "prompt_version >= 11（screen_layout+vlm_hierarchy 每轮必填）"
AGENT_DECIDE_V12_MARKER = "prompt_version >= 12（tool 参数承载 screen_layout）"
AGENT_DECIDE_V13_MARKER = "prompt_version >= 13 (drop forced visual JSON)"
AGENT_DECIDE_V14_MARKER = "prompt_version >= 14 (low-conf nav: don't freeze on wait_ms)"
AGENT_DECIDE_V15_MARKER = "prompt_version >= 15 (do_subphase + thought/action 对齐)"
AGENT_DECIDE_V16_MARKER = "prompt_version >= 16 (swipe_direction from/to 千分比)"
AGENT_DECIDE_V17_MARKER = "prompt_version >= 17 (allow_foreign_foreground_llm_image)"
AGENT_DECIDE_V18_MARKER = "prompt_version >= 18 (prep: pick account/device/cleanup only)"
AGENT_DECIDE_V19_MARKER = "agent-decide prompt v19 (scoped step + session_json)"
DOC_CONTEXT_SLOT = "doc_context"
ASSERT_VISION_V2_MARKER = "prompt_version >= 2（screen_layout 布局线框）"
ASSERT_VISION_V3_MARKER = "prompt_version >= 3 (drop forced visual JSON)"
INSPECT_SESSION_V2_MARKER = "prompt_version >= 2（screen_layout + vlm_hierarchy）"
INSPECT_SESSION_V3_MARKER = "prompt_version >= 3 (drop forced visual JSON)"
NAV_ASSIST_SLOT = "nav_assist"

_SCREEN_LAYOUT_JSON_HINT = """
### screen_layout（与 action 同轮输出，必填对象）

除决策 JSON 外，**必须**附带 `screen_layout`，描述当前屏**内容区**布局（顶/底系统栏与 Tab 栏不要画进 regions）：
- 坐标一律 **0~1 归一化**（相对整屏宽高），与 hierarchy 并行、互不替代。
- `chrome.top` / `chrome.bottom`：内容区上下边界（0~1）。
- `regions[]`：每项 `{"id":"r1","label":"简短语义","role":"banner|feed|dialog|form|...","clickable":true,"rect":{"x":0.05,"y":0.12,"w":0.9,"h":0.08}}`

```json
"screen_layout": {
  "chrome": {"top": 0.06, "bottom": 0.96},
  "regions": [{"id":"r1","label":"引流条","role":"banner","clickable":true,"rect":{"x":0,"y":0.08,"w":1,"h":0.1}}]
}
```
"""

_VLM_HIERARCHY_JSON_HINT = """
### vlm_hierarchy（与 action / 会话字段同轮输出，必填对象）

每轮 JSON **必须**附带 `vlm_hierarchy`（与 `screen_layout` 并列；Scout `==== hierarchy` 是否为空都要写，便于 tap 互证与审计）：
- `hierarchy_format` 固定 `accessibility_json`；`nodes[]` 字段与 Scout `nodes` 同形（`bounds`/`center` 为设备像素）。
- 只列可见且与当前步骤相关的节点（≤40）；不要编造 `resource_id`。
- `degraded_scout`：你认为 Scout hierarchy 不可用/不可信时为 true，否则 false。
- `screen_layout` 管区域框；`vlm_hierarchy` 管可点名控件文案；与 Scout 冲突时以 Scout 坐标为准、VLM 补 text/desc。

```json
"vlm_hierarchy": {
  "hierarchy_format": "accessibility_json",
  "degraded_scout": false,
  "nodes": [{"text":"我的","class":"android.widget.TextView","clickable":true,"bounds":[800,2200,950,2300],"center":[875,2250]}]
}
```
"""


def _commit_job_upgrade(merged: dict[str, Any], job_id: str) -> None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob
    from mino_nexus.services.job_store import _to_row

    with session_scope() as db:
        db_row = db.query(LlmJob).filter(LlmJob.id == job_id).first()
        if db_row is None:
            db.add(_to_row({**merged, "id": job_id, "builtin": True}))
        else:
            db_row.system_blocks_json = list(merged.get("system_blocks") or [])
            db_row.user_blocks_json = list(merged.get("user_blocks") or [])
            db_row.slots_json = list(merged.get("slots") or [])
            db_row.prompt_version = int(merged.get("prompt_version") or 1)
            db_row.overrides_json = dict(merged.get("overrides_json") or {})
            if merged.get("call"):
                db_row.call_json = dict(merged.get("call") or {})
        db.flush()


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


def _patch_agent_decide_v7(text: str) -> str:
    """V6 → V7：告诉模型怎么用【导航 assist】。

    只加「怎么读这段」的规则，**不写任何被测 App 的文案** —— assist 正文全部由
    `nav_compiler` 按 DB 配置现编（设计稿 §0：代码中不得硬编码 App 内容）。
    """
    out = str(text or "")

    nav_block = """### 导航 assist（出现「==== 导航 assist」块时必守）

- 正文由导航图编译，比凭截图猜路更可靠；**有块就按块里写的做**。
- **【导航】**：当前在哪一屏（中文名 + id + 置信度）。置信低时页名仅供参考，以截图和用例步骤为准；达成信号已在屏上则 signal_done，禁止连续空等 wait_ms。
- **【路线】** 三种情况要分清：
  - 「下一步：…」→ 按写的点击去下一屏（仅用于赶路，与用例步骤冲突时以用例为准）。
  - 「已在用例导航目标屏」→ 不必再切 Tab，按用例步骤继续。
  - 「路线图未覆盖」→ **没有已发布的跳转能到用例目标**；不要编造 Tab/返回；按用例步骤点控件，或 signal_ask_human / signal_give_up。
- **【本步允许】**：本步点击白名单（`tap_element` 仅用于【路线】或用例步骤里的控件）。`signal_*` 不受守卫限。
- **【禁止】**：列出的 tap **不要试**（steer → block → 停案）。
- **【滚动】**：锚点不在屏上时先滑再走路线。
- 没有导航 assist 块 → 照常按截图与步骤（多数应用未配图时正常）。

"""
    anchor = "### 前台不是被测 App"
    if anchor in out and "### 导航 assist" not in out:
        out = out.replace(anchor, nav_block + anchor, 1)
    elif "### 导航 assist" not in out:
        out = out.rstrip() + "\n\n" + nav_block

    if AGENT_DECIDE_V7_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {AGENT_DECIDE_V7_MARKER} -->\n"
    return out


def _ensure_nav_assist_slot(merged: dict[str, Any]) -> None:
    """声明槽 + 挂用户块。`_validate_job` 会拒绝引用未声明槽的块，所以两件事必须一起做。"""
    slots = [dict(s) for s in (merged.get("slots") or []) if isinstance(s, dict)]
    if not any(str(s.get("name") or "") == NAV_ASSIST_SLOT for s in slots):
        slots.append({"name": NAV_ASSIST_SLOT, "kind": "text", "desc": "NavFSM RouteAssist（可空）"})
    merged["slots"] = slots

    blocks = [dict(b) for b in (merged.get("user_blocks") or []) if isinstance(b, dict)]
    if not any(str(b.get("slot") or "") == NAV_ASSIST_SLOT for b in blocks):
        blocks.append(
            {
                "id": "nav_assist",
                "slot": NAV_ASSIST_SLOT,
                "heading": "==== 导航 assist（按本应用导航图编译，优先于你的猜测）====",
                # 没配导航图 / 开关关着时槽为空，整块跳过，prompt 一个字都不多
                "skip_if_empty": True,
                "enabled": True,
                "max_chars": 2400,
            }
        )
    merged["user_blocks"] = blocks


def upgrade_agent_decide_to_v7() -> int:
    """已有 agent-decide 且 prompt_version < 7 时，加 nav_assist 槽与用法说明。"""
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    ver = int(row.get("prompt_version") or 1)
    if ver >= 7:
        return 0
    if ver < 6:
        upgrade_agent_decide_to_v6()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v7(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    _ensure_nav_assist_slot(merged)

    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 6),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": f"v{row.get('prompt_version')} before program upgrade to v7",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 7
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
            db_row.slots_json = list(merged.get("slots") or [])
            db_row.prompt_version = 7
            db_row.overrides_json = dict(merged.get("overrides_json") or {})
        db.flush()
    return 1


def _patch_agent_decide_v8(text: str) -> str:
    out = str(text or "")
    if "### screen_layout" in out:
        return out
    anchor = AGENT_DECIDE_V7_MARKER
    if anchor in out:
        out = out.replace(anchor, _SCREEN_LAYOUT_JSON_HINT.strip() + "\n\n<!-- " + anchor + " -->\n")
    else:
        out = out.rstrip() + "\n\n" + _SCREEN_LAYOUT_JSON_HINT.strip() + f"\n\n<!-- {AGENT_DECIDE_V8_MARKER} -->\n"
    return out


def upgrade_agent_decide_to_v8() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 8:
        return 0
    if int(row.get("prompt_version") or 1) < 7:
        upgrade_agent_decide_to_v7()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v8(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 7),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": f"v{row.get('prompt_version')} before program upgrade to v8",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 8
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
            db_row.prompt_version = 8
        db.flush()
    return 1


def _patch_agent_decide_v9(text: str) -> str:
    out = str(text or "")
    doc_block = """### 文档摘录（有【文档摘录】段时）

- 来自上传的 PRD / 说明文档，**仅供参考**；与当前屏或步骤冲突时以截图和步骤原文为准。
- 只采纳能在界面上**验证**的口径（耗时、数量、文案、流程分支）。
- 摘录不完整时不要臆造；需要更多上下文可继续按步骤操作或 signal_ask_human。

"""
    anchor = AGENT_DECIDE_V8_MARKER
    if anchor in out and "### 文档摘录" not in out:
        out = out.replace(anchor, doc_block + "<!-- " + anchor + " -->\n")
    elif "### 文档摘录" not in out:
        out = out.rstrip() + "\n\n" + doc_block + f"<!-- {AGENT_DECIDE_V9_MARKER} -->\n"
    return out


def _ensure_doc_context_slot(merged: dict[str, Any]) -> None:
    slots = [dict(s) for s in (merged.get("slots") or []) if isinstance(s, dict)]
    if not any(str(s.get("name") or "") == DOC_CONTEXT_SLOT for s in slots):
        slots.append({"name": DOC_CONTEXT_SLOT, "kind": "text", "desc": "文档库 FTS 摘录（可空）"})
    merged["slots"] = slots
    blocks = [dict(b) for b in (merged.get("user_blocks") or []) if isinstance(b, dict)]
    if not any(str(b.get("slot") or "") == DOC_CONTEXT_SLOT for b in blocks):
        blocks.append({
            "id": "doc_context",
            "slot": DOC_CONTEXT_SLOT,
            "heading": "==== 文档摘录（上传文档检索，仅供参考）====",
            "skip_if_empty": True,
            "enabled": True,
            "max_chars": 1400,
        })
    merged["user_blocks"] = blocks


def upgrade_agent_decide_to_v9() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 9:
        return 0
    if int(row.get("prompt_version") or 1) < 8:
        upgrade_agent_decide_to_v8()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v9(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    _ensure_doc_context_slot(merged)
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 8),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": f"v{row.get('prompt_version')} before program upgrade to v9",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 9
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
            db_row.slots_json = list(merged.get("slots") or [])
            db_row.prompt_version = 9
        db.flush()
    return 1


def _patch_agent_decide_v10(text: str) -> str:
    out = str(text or "")
    if "### vlm_hierarchy" in out:
        return out
    anchor = AGENT_DECIDE_V9_MARKER
    if anchor in out:
        out = out.replace(anchor, _VLM_HIERARCHY_JSON_HINT.strip() + "\n\n<!-- " + anchor + " -->\n")
    else:
        out = out.rstrip() + "\n\n" + _VLM_HIERARCHY_JSON_HINT.strip() + f"\n\n<!-- {AGENT_DECIDE_V10_MARKER} -->\n"
    return out


def upgrade_agent_decide_to_v10() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 10:
        return 0
    if int(row.get("prompt_version") or 1) < 9:
        upgrade_agent_decide_to_v9()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v10(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    _ensure_doc_context_slot(merged)
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 9),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v9 before program upgrade to v10",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 10
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


def _patch_agent_decide_v11(text: str) -> str:
    """V10 → V11：vlm_hierarchy 改为每轮必填；统一 screen_layout 话术。"""
    out = str(text or "")
    out = re.sub(
        r"### vlm_hierarchy[\s\S]*?(?=\n### |\n<!-- prompt_version|\Z)",
        _VLM_HIERARCHY_JSON_HINT.strip() + "\n\n",
        out,
        count=1,
    )
    if "### vlm_hierarchy" not in out:
        out = out.rstrip() + "\n\n" + _VLM_HIERARCHY_JSON_HINT.strip() + "\n"
    if "### screen_layout" not in out:
        out = out.rstrip() + "\n\n" + _SCREEN_LAYOUT_JSON_HINT.strip() + "\n"
    if AGENT_DECIDE_V11_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {AGENT_DECIDE_V11_MARKER} -->\n"
    return out


_AGENT_DECIDE_V12_TOOL_LAYOUT_NOTE = """
### 输出通道（agent-decide 使用 function call）

`screen_layout` 与 `vlm_hierarchy` **写在本次 function call 的参数里**（与 `thought`、能力参数同级），不要只写在正文。
若模型同时输出 assistant 正文 JSON，服务端会合并补全；但 **以 tool 参数为准**。
"""


def _patch_agent_decide_v12(text: str) -> str:
    out = str(text or "")
    if _AGENT_DECIDE_V12_TOOL_LAYOUT_NOTE.strip() not in out:
        out = out.rstrip() + "\n\n" + _AGENT_DECIDE_V12_TOOL_LAYOUT_NOTE.strip() + "\n"
    if AGENT_DECIDE_V12_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {AGENT_DECIDE_V12_MARKER} -->\n"
    return out


def upgrade_agent_decide_to_v12() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 12:
        return 0
    if int(row.get("prompt_version") or 1) < 11:
        upgrade_agent_decide_to_v11()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v12(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 11),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": f"v{row.get('prompt_version')} before program upgrade to v12 tool-layout channel",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 12
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


def _strip_forced_visual_json(text: str) -> str:
    """拿掉 v8–v12 要求每轮吐 screen_layout / vlm_hierarchy 的章节。"""
    out = str(text or "")
    out = re.sub(
        r"\n*### screen_layout[\s\S]*?(?=\n### |\n<!-- prompt_version|\Z)",
        "\n",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"\n*### vlm_hierarchy[\s\S]*?(?=\n### |\n<!-- prompt_version|\Z)",
        "\n",
        out,
        flags=re.I,
    )
    out = re.sub(
        r"\n*### 输出通道[\s\S]*?(?=\n### |\n<!-- prompt_version|\Z)",
        "\n",
        out,
    )
    return out.rstrip() + "\n"


def _patch_agent_decide_v13(text: str) -> str:
    out = _strip_forced_visual_json(text)
    drop_note = (
        "### 不要输出布局 JSON\n\n"
        "Scout 已注入 hierarchy。不要输出 screen_layout、vlm_hierarchy，"
        "也不要在 tool 参数里填这两项。只输出 thought 与能力参数。\n"
    )
    if "### 不要输出布局 JSON" not in out:
        out = out.rstrip() + "\n\n" + drop_note
    if AGENT_DECIDE_V13_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {AGENT_DECIDE_V13_MARKER} -->\n"
    return out


def _cap_call_tokens(merged: dict[str, Any], max_tokens: int) -> None:
    call = dict(merged.get("call") or {})
    call["max_tokens"] = int(max_tokens)
    merged["call"] = call


def upgrade_agent_decide_to_v13() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 13:
        return 0
    if int(row.get("prompt_version") or 1) < 12:
        upgrade_agent_decide_to_v12()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v13(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    _cap_call_tokens(merged, 768)
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 12),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v12 before drop forced screen_layout/vlm_hierarchy",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 13
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


_AGENT_DECIDE_V14_OLD = "置信低时先 recover / 问人，勿乱点。"
_AGENT_DECIDE_V14_NEW = (
    "置信低时页名仅供参考，以截图和用例步骤为准；"
    "达成信号已在屏上则 signal_done，禁止连续空等 wait_ms。"
)


def _patch_agent_decide_v14(text: str) -> str:
    out = str(text or "")
    if _AGENT_DECIDE_V14_OLD in out:
        out = out.replace(_AGENT_DECIDE_V14_OLD, _AGENT_DECIDE_V14_NEW, 1)
    if AGENT_DECIDE_V14_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {AGENT_DECIDE_V14_MARKER} -->\n"
    return out


_DO_SUBPHASE_HINT = """
### do 步子阶段（operation / achievement）

步骤块会标明当前子阶段：
- **operation**：只执行 instruction 与【本步导航】；「达成信号」仅为摘要，**禁止**当作点击/输入目标。
- **achievement**：只判断本步达成信号可否 `signal_done`；**禁止**再 mutate 改界面。
- `thought` 结论须与 `action.capability_id` 一致；应收工时必须 `signal_done`，勿 thought 写收工却发 tap/press_key/BACK。
"""


def _patch_agent_decide_v15(text: str) -> str:
    out = str(text or "")
    if _DO_SUBPHASE_HINT.strip() not in out:
        out = out.rstrip() + "\n\n" + _DO_SUBPHASE_HINT.strip() + "\n"
    if AGENT_DECIDE_V15_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {AGENT_DECIDE_V15_MARKER} -->\n"
    return out


_SWIPE_DIRECTION_HINT = """
### swipe_direction（横条 / 列表 / 局部滚动）

- 底部风格条、横向 Tab、RecyclerView 列表等**局部可滑区域**：必须根据截图/`screen_layout` 给出 **`from_x, from_y, to_x, to_y`（0–1000 千分比）**，沿目标控件滑动。
- **禁止**仅靠 `direction` 在**屏中心**做全屏滑动手势（常带不动横条、造成空转）。
- 若目标文案已在屏上可见，优先 **`tap_element`** 点选，不必盲滑。
"""


def _patch_agent_decide_v16(text: str) -> str:
    out = str(text or "")
    if _SWIPE_DIRECTION_HINT.strip() not in out:
        out = out.rstrip() + "\n\n" + _SWIPE_DIRECTION_HINT.strip() + "\n"
    if AGENT_DECIDE_V16_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {AGENT_DECIDE_V16_MARKER} -->\n"
    return out


_FOREIGN_FG_IMAGE_HINT = """
### allow_foreign_foreground_llm_image（送图准入，与 session 字段同级）

`【执行上下文·送图】` 中 `app_foreground=no` 时，本回合可能**未附截图**；若必须分析当前系统/第三方屏，在 JSON 根设 `"allow_foreign_foreground_llm_image": true`，系统会用同屏截图重试决策一次。
分析完成后设 `false` 或勿再申请。默认 withhold，勿假设每轮都有图。
"""


def _patch_agent_decide_v17(text: str) -> str:
    out = str(text or "")
    if _FOREIGN_FG_IMAGE_HINT.strip() not in out:
        out = out.rstrip() + "\n\n" + _FOREIGN_FG_IMAGE_HINT.strip() + "\n"
    if AGENT_DECIDE_V17_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {AGENT_DECIDE_V17_MARKER} -->\n"
    return out


def upgrade_agent_decide_to_v17() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 17:
        return 0
    if int(row.get("prompt_version") or 1) < 16:
        upgrade_agent_decide_to_v16()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v17(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 16),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v16 before foreign foreground llm image gate",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 17
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


_prep_flow_v18_hint = """
### 前置三件事（不要切换测试环境）

前置阶段只做：**筛选账号**（lease_account）、**筛选设备**、**环境清理**（clear_app_cache 等）。
禁止 `check_run_env` / 切换测试环境——批次 `env_profile` 已经确定环境。
设备登录态（机态）与账号登录态（号池 session）不是同一把钥匙；不要用号池去核手机是否登录。
"""


def _patch_agent_decide_v18(text: str) -> str:
    out = str(text or "")
    if "前置三件事" not in out:
        out = out.rstrip() + "\n\n" + _prep_flow_v18_hint.strip() + "\n"
    if AGENT_DECIDE_V18_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {AGENT_DECIDE_V18_MARKER} -->\n"
    return out


def upgrade_agent_decide_to_v18() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 18:
        return 0
    if int(row.get("prompt_version") or 1) < 17:
        upgrade_agent_decide_to_v17()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v18(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 17),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v17 before prep pick account/device/cleanup only",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 18
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


_PREP_V19_HINT = """
### 本步上下文（v19）

- 只看 `checkpoints_block` / `success_criteria`（JSON，含 `milestones`）/ `history_block`（**本步本阶段**操作）。
- `session_json`、`accounts_json` 为结构化登录态与租号信息；勿再依赖 device 摘要。
- 能力以 function tools 为准；`menu_json` / `device_brief_json` 若出现可忽略。
"""


def _patch_agent_decide_v19(text: str) -> str:
    out = str(text or "")
    out = re.sub(r"\n*<!--\s*prompt_version[^>]*-->\s*", "\n", out, flags=re.IGNORECASE)
    out = re.sub(
        r"====\s*device[^\n]*\n[\s\S]*?====\s*session",
        "==== session",
        out,
        count=1,
        flags=re.IGNORECASE,
    )
    out = out.replace("{{device_brief_json}}", "")
    out = out.replace("{{accounts_brief}}", "{{accounts_json}}")
    if "{{session_json}}" not in out and "{{session_block}}" in out:
        out = out.replace("{{session_block}}", "{{session_json}}")
    if AGENT_DECIDE_V19_MARKER not in out:
        out = out.rstrip() + "\n\n" + _PREP_V19_HINT.strip() + f"\n\n({AGENT_DECIDE_V19_MARKER})\n"
    return out


def upgrade_agent_decide_to_v19() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 19:
        return 0
    if int(row.get("prompt_version") or 1) < 18:
        upgrade_agent_decide_to_v18()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v19(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 18),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v18 before scoped step context / session_json",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 19
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


def _rebind_agent_decide_user_blocks(user_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rebind = {
        "session_block": ("session_json", "==== 会话状态 JSON（session_json）===="),
        "accounts_brief": ("accounts_json", "==== 租号/账号 JSON（accounts_json）===="),
    }
    disable = {"device_brief_json", "menu_json"}
    out: list[dict[str, Any]] = []
    for block in user_blocks or []:
        if not isinstance(block, dict):
            continue
        b = dict(block)
        slot = str(b.get("slot") or "").strip()
        if slot in rebind:
            new_slot, heading = rebind[slot]
            b["slot"] = new_slot
            b["heading"] = heading
        if slot in disable:
            b["enabled"] = False
            b.pop("slot", None)
        out.append(b)
    return out


def _rebind_agent_decide_slot_specs(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rename = {"session_block": "session_json", "accounts_brief": "accounts_json"}
    drop = {"device_brief_json", "menu_json"}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for spec in slots or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "").strip()
        if not name or name in drop:
            continue
        name = rename.get(name, name)
        if name in seen:
            continue
        seen.add(name)
        out.append({**spec, "name": name})
    for required in ("session_json", "accounts_json"):
        if required not in seen:
            out.append({"name": required, "kind": "text", "required": False})
            seen.add(required)
    return out


def upgrade_agent_decide_to_v20() -> int:
    """v19 只改了 system 文案；v20 绑定 user_blocks 槽到 session_json / accounts_json。"""
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 20:
        return 0
    if int(row.get("prompt_version") or 1) < 19:
        upgrade_agent_decide_to_v19()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    merged["user_blocks"] = _rebind_agent_decide_user_blocks(list(merged.get("user_blocks") or []))
    merged["slots"] = _rebind_agent_decide_slot_specs(list(merged.get("slots") or []))
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 19),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v19 before user_blocks slot rebind (session_json)",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 20
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


AGENT_DECIDE_V21_MARKER = "agent-decide prompt v21 (milestones + milestone_updates)"

_PREP_V21_HINT = """
### 子里程碑（v21）

- `success_criteria` JSON 中 `milestones[]` 为**本步本 phase**进度真源。
- **首轮**本步尚无里程碑时，在 JSON 输出中增加 `milestones` 数组（3–8 条，含 `id`/`title`/`kind`/`status`）。
- 每回合用 `milestone_updates`: `[{ "id", "status": "pass|pending|failed|skipped", "evidence" }]` 更新状态；可选 `step_outcome`。
- check 阶段：以里程碑全部 pass（或 optional 为 skipped）为准，再 `signal_done`；勿依赖 assert_visual。
"""


def _patch_agent_decide_v21_system(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = copy.deepcopy(blocks)
    for spec in out:
        if str(spec.get("name") or "") != "system":
            continue
        body = str(spec.get("body") or "")
        if AGENT_DECIDE_V21_MARKER in body:
            return out
        spec["body"] = body.rstrip() + "\n\n" + _PREP_V21_HINT.strip() + f"\n\n({AGENT_DECIDE_V21_MARKER})\n"
        break
    return out


def upgrade_agent_decide_to_v21() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 21:
        return 0
    if int(row.get("prompt_version") or 1) < 20:
        upgrade_agent_decide_to_v20()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    merged["system_blocks"] = _patch_agent_decide_v21_system(list(merged.get("system_blocks") or []))
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 20),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v20 before milestones output hint",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 21
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


AGENT_DECIDE_V22_MARKER = "agent-decide prompt v22 (function tools; drop tool markdown table)"

_TOOL_MARKDOWN_TABLE_RE = re.compile(
    r"\n\| 工具 \| 参数 \|\n\|[-:]+\|[-:]+\|\n(?:\|[^\n]+\|\n)+",
    re.MULTILINE,
)

_PREP_V22_HINT = """
### 能力调用（v22）

- 正文不再维护「工具 | 参数」Markdown 表；**只**调用本轮下发的 function tools（参数以 schema 为准）。
- **首轮**本步尚无子里程碑时，必须在 JSON 中输出 `milestones`（3–8 条，`id`/`title`/`kind`/`status`）；登录步系统可能将 FSM 逻辑块插入列表顶部，仍用 `milestone_updates` + 工具执行推进。
"""


def _strip_agent_decide_tool_markdown_table(text: str) -> str:
    out = str(text or "")
    if _TOOL_MARKDOWN_TABLE_RE.search(out):
        out = _TOOL_MARKDOWN_TABLE_RE.sub(
            "\n\n设备能力以本轮 **function tools** 为准；勿调用未下发的工具。\n",
            out,
            count=1,
        )
    return out


def _patch_agent_decide_v22_system(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = copy.deepcopy(blocks)
    for spec in out:
        name = str(spec.get("name") or "")
        if name not in ("system", ""):
            continue
        body_key = "body" if "body" in spec else "text"
        body = str(spec.get(body_key) or "")
        if AGENT_DECIDE_V22_MARKER in body:
            return out
        body = _strip_agent_decide_tool_markdown_table(body)
        if AGENT_DECIDE_V22_MARKER not in body:
            body = body.rstrip() + "\n\n" + _PREP_V22_HINT.strip() + f"\n\n({AGENT_DECIDE_V22_MARKER})\n"
        spec[body_key] = body
        break
    return out


def upgrade_agent_decide_to_v22() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 22:
        return 0
    if int(row.get("prompt_version") or 1) < 21:
        upgrade_agent_decide_to_v21()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    merged["system_blocks"] = _patch_agent_decide_v22_system(list(merged.get("system_blocks") or []))
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 21),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v21 before strip tool markdown table",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 22
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


def upgrade_agent_decide_to_v16() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 16:
        return 0
    if int(row.get("prompt_version") or 1) < 15:
        upgrade_agent_decide_to_v15()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v16(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 15),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v15 before swipe_direction from/to milli coords",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 16
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


def upgrade_agent_decide_to_v15() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 15:
        return 0
    if int(row.get("prompt_version") or 1) < 14:
        upgrade_agent_decide_to_v14()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v15(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 14),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v14 before do_subphase + thought/action alignment",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 15
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


def upgrade_agent_decide_to_v14() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 14:
        return 0
    if int(row.get("prompt_version") or 1) < 13:
        upgrade_agent_decide_to_v13()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v14(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 13),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v13 before low-conf nav: don't freeze on wait_ms",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 14
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


def upgrade_agent_decide_to_v11() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 11:
        return 0
    if int(row.get("prompt_version") or 1) < 10:
        upgrade_agent_decide_to_v10()
        row = get_job("agent-decide") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_agent_decide_v11(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    _ensure_doc_context_slot(merged)
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 10),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": f"v{row.get('prompt_version')} before program upgrade to v11",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 11
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


def _patch_inspect_session_v2(text: str) -> str:
    out = str(text or "")
    needle = '"reason": "一句话"\n}'
    extended = (
        '"reason": "一句话",\n'
        '  "screen_layout": {"chrome": {"top": 0.06, "bottom": 0.96}, "regions": []},\n'
        '  "vlm_hierarchy": {"hierarchy_format": "accessibility_json", "degraded_scout": false, "nodes": []}\n'
        "}"
    )
    if needle in out and '"screen_layout"' not in out[:2000]:
        out = out.replace(needle, extended, 1)
    if "### screen_layout" not in out:
        out = (
            out.rstrip()
            + "\n\n"
            + _SCREEN_LAYOUT_JSON_HINT.strip()
            + "\n\n"
            + _VLM_HIERARCHY_JSON_HINT.strip()
            + f"\n\n<!-- {INSPECT_SESSION_V2_MARKER} -->\n"
        )
    return out


def upgrade_inspect_session_to_v2() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("inspect-session")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 2:
        return 0

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_inspect_session_v2(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v1 before program upgrade to v2 screen_layout+vlm_hierarchy",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 2
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "inspect-session")
    return 1


def _patch_inspect_session_v3(text: str) -> str:
    out = _strip_forced_visual_json(text)
    drop_note = (
        "### 不要输出布局 JSON\n\n"
        "不要输出 screen_layout、vlm_hierarchy。只输出 session / identity / next / reason。\n"
    )
    if "### 不要输出布局 JSON" not in out:
        out = out.rstrip() + "\n\n" + drop_note
    if INSPECT_SESSION_V3_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {INSPECT_SESSION_V3_MARKER} -->\n"
    return out


def upgrade_inspect_session_to_v3() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("inspect-session")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 3:
        return 0
    if int(row.get("prompt_version") or 1) < 2:
        upgrade_inspect_session_to_v2()
        row = get_job("inspect-session") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_inspect_session_v3(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    _cap_call_tokens(merged, 320)
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 2),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v2 before drop forced screen_layout/vlm_hierarchy",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 3
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "inspect-session")
    return 1


def _patch_assert_vision_v2(text: str) -> str:
    out = str(text or "")
    if "### screen_layout" in out:
        return out
    return out.rstrip() + "\n\n" + _SCREEN_LAYOUT_JSON_HINT.strip() + f"\n\n<!-- {ASSERT_VISION_V2_MARKER} -->\n"


def upgrade_assert_vision_to_v2() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("assert-vision")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 2:
        return 0
    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_assert_vision_v2(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "before program upgrade to v2 screen_layout",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 2
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)

    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob
    from mino_nexus.services.job_store import _to_row

    _commit_job_upgrade(merged, "assert-vision")
    return 1


def _patch_assert_vision_v3(text: str) -> str:
    out = _strip_forced_visual_json(text)
    drop_note = (
        "### 不要输出布局 JSON\n\n"
        "不要输出 screen_layout。只输出 passed / confidence / evidence / reasoning。\n"
    )
    if "### 不要输出布局 JSON" not in out:
        out = out.rstrip() + "\n\n" + drop_note
    if ASSERT_VISION_V3_MARKER not in out:
        out = out.rstrip() + f"\n\n<!-- {ASSERT_VISION_V3_MARKER} -->\n"
    return out


def upgrade_assert_vision_to_v3() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("assert-vision")
    if not row:
        return 0
    if int(row.get("prompt_version") or 1) >= 3:
        return 0
    if int(row.get("prompt_version") or 1) < 2:
        upgrade_assert_vision_to_v2()
        row = get_job("assert-vision") or row

    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    if not blocks:
        return 0
    main = dict(blocks[0])
    main["text"] = _patch_assert_vision_v3(str(main.get("text") or ""))
    blocks[0] = main
    merged["system_blocks"] = blocks
    _cap_call_tokens(merged, 320)
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 2),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v2 before drop forced screen_layout",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 3
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "assert-vision")
    return 1


def _repair_nav_widget_state_slots(row: dict[str, Any]) -> int:
    """历史 seed 误用 slots[].id，启动时归一成 name。"""
    fixed: list[dict[str, Any]] = []
    changed = False
    for spec in row.get("slots") or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or spec.get("id") or "").strip()
        if not name:
            continue
        entry = {"name": name, "kind": str(spec.get("kind") or "text")}
        if spec.get("name") != name or spec.get("id"):
            changed = True
        fixed.append(entry)
    if not changed:
        return 0
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob
    from mino_nexus.services.job_store import _validate_job

    row["slots"] = fixed
    _validate_job(row)
    with session_scope() as db:
        db_row = db.query(LlmJob).filter(LlmJob.id == "nav-widget-state").first()
        if db_row is None:
            return 0
        db_row.slots_json = fixed
        db.flush()
    return 1


def ensure_nav_widget_state_job() -> int:
    """可选 job：hierarchy 判不出控件态时 VLM 兜底（设计稿 §2.2）。缺了不报错，只是兜底不开。"""
    from mino_nexus.services.job_store import get_job, _to_row

    jid = "nav-widget-state"
    existing = get_job(jid)
    if existing:
        return _repair_nav_widget_state_slots(existing)
    spec = {
        "id": jid,
        "label": "Nav 控件态判定",
        "summary": "单控件 VLM 兜底：空心/实心等，非整屏分类",
        "engine": "text_chat",
        "enabled": True,
        "builtin": True,
        "prompt_version": 1,
        "output_schema": "json",
        "slots": [
            {"name": "widget", "kind": "text"},
            {"name": "candidate_states", "kind": "text"},
            {"name": "hint", "kind": "text"},
            {"name": "image_base64", "kind": "image"},
            {"name": "image_mime", "kind": "text"},
        ],
        "system_blocks": [
            {
                "id": "main",
                "text": (
                    "你是 UI 控件态判定器。只看截图里指定控件处于哪个态。\n"
                    "候选态：{{candidate_states}}\n"
                    "控件：{{widget}}\n"
                    "{{hint}}\n"
                    "只输出 JSON：{\"state\":\"候选之一\",\"confidence\":0-1,\"reason\":\"\"}"
                ),
            }
        ],
        "user_blocks": [{"id": "img", "kind": "image", "slot": "image_base64", "mime_slot": "image_mime"}],
        "call": {"temperature": 0.0, "max_tokens": 200, "timeout_sec": 30, "json_mode": True},
        "flags": ["case_execution_use"],
    }
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob

    with session_scope() as db:
        if db.query(LlmJob).filter(LlmJob.id == jid).first():
            return 0
        db.add(_to_row(spec))
        db.flush()
    return 1


AGENT_VISION_EXEC_V1_MARKER = "agent-vision-exec v1 (executor only; no milestones)"


def ensure_agent_vision_exec_job() -> int:
    """P6-P1：从 agent-decide 克隆看图执行 Job（瘦上下文 + 禁里程碑输出）。"""
    import copy

    from mino_nexus.services.job_store import _to_row, _validate_job, get_job

    jid = "agent-vision-exec"
    if get_job(jid):
        return 0
    src = get_job("agent-decide")
    if not src:
        return 0
    spec = copy.deepcopy(src)
    spec["id"] = jid
    spec["label"] = "看图执行"
    spec["summary"] = "按 plan_digest 输出单步 capability；里程碑由 agent-vision-plan 维护"
    spec["prompt_version"] = 1
    spec["builtin"] = True
    spec["enabled"] = True
    slot_names = {str(s.get("name") or "") for s in (spec.get("slots") or [])}
    for name in ("plan_digest", "active_milestone", "phase"):
        if name not in slot_names:
            spec["slots"] = list(spec.get("slots") or []) + [{"name": name, "kind": "text"}]
    spec["user_blocks"] = [
        {"id": "plan", "slot": "plan_digest", "heading": "==== plan_digest ===="},
        {"id": "am", "slot": "active_milestone", "heading": "==== active_milestone ===="},
        {"id": "goal", "slot": "goal", "heading": "==== goal ===="},
        {"id": "hist", "slot": "history_block", "heading": "==== history ===="},
        {"id": "sess", "slot": "session_json", "heading": "==== session ===="},
        {"id": "acc", "slot": "accounts_json", "heading": "==== accounts ===="},
        {"id": "nav", "slot": "nav_assist", "heading": "==== nav ====", "skip_if_empty": True},
        {"id": "know", "slot": "knowledge_body", "heading": "==== knowledge ====", "skip_if_empty": True},
        {"id": "doc", "slot": "doc_context", "heading": "==== doc ====", "skip_if_empty": True},
        {"id": "hier", "slot": "hierarchy_text", "heading": "==== hierarchy ====", "skip_if_empty": True},
        {"id": "img", "kind": "image", "slot": "image_base64", "mime_slot": "image_mime"},
    ]
    blocks = copy.deepcopy(list(spec.get("system_blocks") or []))
    addon = (
        "\n\n### 看图执行（agent-vision-exec）\n"
        "你是**执行器**：根据 plan_digest、active_milestone 与截图，调用 function tools 完成**一步**设备操作。\n"
        "禁止在 JSON 中输出 milestones、milestone_updates、step_outcome；"
        "禁止 signal_done / signal_give_up 作为收工（程序按里程碑聚合流转）。\n"
        f"({AGENT_VISION_EXEC_V1_MARKER})\n"
    )
    for sb in blocks:
        key = "body" if "body" in sb else "text"
        body = str(sb.get(key) or "")
        if AGENT_VISION_EXEC_V1_MARKER not in body:
            sb[key] = body.rstrip() + addon
        break
    else:
        blocks.append({"id": "main", "text": addon.strip()})
    spec["system_blocks"] = blocks
    _validate_job(spec)
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob

    with session_scope() as db:
        if db.query(LlmJob).filter(LlmJob.id == jid).first():
            return 0
        db.add(_to_row(spec))
        db.flush()
    return 1


def ensure_account_facet_commit_job() -> int:
    """用例结束：根据执行轨迹推断号池 facet 写回（pass/fail/超时均调用）。"""
    from mino_nexus.services.job_store import get_job, _to_row

    jid = "account-facet-commit"
    if get_job(jid):
        return 0
    spec = {
        "id": jid,
        "label": "账号 Facet 写回分析",
        "summary": "读用例 timeline，判断号池 facet 应更新哪些字段",
        "engine": "json_chat",
        "role_id": "test-engineer",
        "enabled": True,
        "builtin": True,
        "prompt_version": 1,
        "output_schema": "json",
        "slots": [{"name": "payload_json", "kind": "text"}],
        "system_blocks": [
            {
                "id": "main",
                "text": (
                    "你是测试账号池状态写回分析器。根据 JSON 里的执行 timeline、探针与字段 catalog，"
                    "判断租用账号应更新哪些 facet。\n\n"
                    "铁律：\n"
                    "- 只写有**执行证据**的字段；无证据则 updates 必须为 {}\n"
                    "- 仅进入形象/资料配置页、未点保存/完成 → 不要写已配置形象\n"
                    "- 轨迹或 assert 明确保存成功并进入后续业务页 → 可写形象类为 yes/已配置\n"
                    "- 登录/登出以 timeline 中 login/logout/clear_app_cache/inspect 为准，不靠前置假设\n"
                    "- 用例 fail 或超时，若轨迹已证明某状态达成，仍可在 updates 中写该状态\n"
                    "- updates 的 key/value 必须来自 field_catalog 的枚举\n"
                    "- author_facet_hints 仅供参考，不能替代证据\n\n"
                    "只输出 JSON：\n"
                    '{"updates":{},"reason":"简短中文","confidence":"low|medium|high"}'
                ),
            }
        ],
        "user_blocks": [
            {
                "id": "payload",
                "slot": "payload_json",
                "heading": "==== 用例与轨迹 ====",
            }
        ],
        "call": {"temperature": 0.1, "max_tokens": 700, "timeout_sec": 45, "json_mode": True},
        "flags": ["case_execution_use"],
    }
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob

    with session_scope() as db:
        if db.query(LlmJob).filter(LlmJob.id == jid).first():
            return 0
        db.add(_to_row(spec))
        db.flush()
    return 1


def ensure_nav_atlas_morph_job() -> int:
    """M4：两帧/线框上下文 + 截图，判定 same_page_morph | split_page。"""
    from mino_nexus.services.job_store import get_job, _to_row

    jid = "nav-atlas-morph"
    if get_job(jid):
        return 0
    spec = {
        "id": jid,
        "label": "Atlas 多态判定",
        "summary": "同一逻辑页内容多态 vs 应拆页（仅建议 pin/split）",
        "engine": "text_chat",
        "enabled": True,
        "builtin": True,
        "prompt_version": 1,
        "output_schema": "json",
        "slots": [
            {"name": "context_json", "kind": "text"},
            {"name": "image_base64", "kind": "image"},
            {"name": "image_mime", "kind": "text"},
        ],
        "system_blocks": [
            {
                "id": "main",
                "text": (
                    "你是移动 App **架构采集**审核员。根据上下文 JSON（两 turn 的线框摘要、localize）"
                    "和用户附带的**较新一帧截图**，判断这两帧是否应算作**同一逻辑页的多态 morph**。\n\n"
                    "只输出 JSON：\n"
                    '{"verdict":"same_page_morph|split_page","confidence":0-1,"reason":""}\n\n'
                    "- **same_page_morph**：壳层/导航结构相同，仅 feed/轮播/列表内容变化；应 pin 到同一 page.sk。\n"
                    "- **split_page**：壳层、返回栈、Tab 或主布局角色变化；应拆成独立页。\n"
                    "不要编造未看见的控件；不确定时 split_page 且 confidence<0.6。"
                ),
            }
        ],
        "user_blocks": [
            {
                "id": "ctx",
                "slot": "context_json",
                "heading": "==== 两帧上下文（JSON）====",
            }
        ],
        "image": {"slot": "image_base64"},
        "call": {"temperature": 0.0, "max_tokens": 280, "timeout_sec": 45, "json_mode": True},
        "flags": ["case_execution_use"],
    }
    # user_blocks 需要 image 块由 render_job 的 image 配置处理
    spec["user_blocks"] = list(spec["user_blocks"])
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob

    with session_scope() as db:
        if db.query(LlmJob).filter(LlmJob.id == jid).first():
            return 0
        db.add(_to_row(spec))
        db.flush()
    return 1


def ensure_agent_vision_plan_job() -> int:
    """P6-P0：看图规划 Job（不下发设备 cap）。执行仍走 agent-decide。"""
    from mino_nexus.services.job_store import get_job, _to_row

    jid = "agent-vision-plan"
    if get_job(jid):
        return 0
    spec = {
        "id": jid,
        "label": "看图规划",
        "summary": "本回合里程碑与逻辑块编排；不输出 tap/input",
        "engine": "json_chat",
        "role_id": "test-engineer",
        "enabled": True,
        "builtin": True,
        "prompt_version": 1,
        "output_schema": "json",
        "slots": [
            {"name": "phase", "kind": "text"},
            {"name": "phase_step_text", "kind": "text"},
            {"name": "checkpoints_block", "kind": "text"},
            {"name": "history_block", "kind": "text"},
            {"name": "success_criteria", "kind": "text"},
            {"name": "target_app", "kind": "text"},
            {"name": "session_json", "kind": "text"},
            {"name": "accounts_json", "kind": "text"},
            {"name": "screen_size_block", "kind": "text"},
            {"name": "hierarchy_text", "kind": "text"},
            {"name": "nav_assist", "kind": "text"},
            {"name": "knowledge_hint", "kind": "text"},
            {"name": "knowledge_body", "kind": "text"},
            {"name": "doc_context", "kind": "text"},
            {"name": "review_program_fail", "kind": "text"},
            {"name": "image_base64", "kind": "image"},
            {"name": "image_mime", "kind": "text"},
        ],
        "system_blocks": [
            {
                "id": "main",
                "text": (
                    "你是测试执行**规划器**（agent-vision-plan）。根据截图与上下文，规划本回合要做的子里程碑、"
                    "逻辑块步骤与 skip，**不要**输出任何设备 capability（tap/input/swipe 等）。\n\n"
                    "阶段 phase={{phase}}\n"
                    "本阶段步骤文案：\n{{phase_step_text}}\n\n"
                    "铁律：\n"
                    "- prep/do：可输出 milestones、flow_block_ops、hook_calls、plan_digest\n"
                    "- check：只输出 checkpoints_plan 与对应 milestones（kind=checkpoint）\n"
                    "- 若 review_program_fail 非空：输出 failure_verdict.blocking 与 reason（复核程序标 failed）\n"
                    "- 不要 signal_done / milestone_updates 字段（程序写回状态）\n\n"
                    "只输出 JSON：\n"
                    '{"thought":"","milestones":[],"flow_block_ops":[],"hook_calls":[],'
                    '"checkpoints_plan":[],"plan_digest":{},"failure_verdict":{}}'
                ),
            }
        ],
        "user_blocks": [
            {"id": "ctx", "slot": "checkpoints_block", "heading": "==== checkpoints ===="},
            {"id": "hist", "slot": "history_block", "heading": "==== history ===="},
            {"id": "sc", "slot": "success_criteria", "heading": "==== success_criteria ===="},
            {"id": "sess", "slot": "session_json", "heading": "==== session ===="},
            {"id": "acc", "slot": "accounts_json", "heading": "==== accounts ===="},
            {"id": "nav", "slot": "nav_assist", "heading": "==== nav ====", "skip_if_empty": True},
            {"id": "know", "slot": "knowledge_body", "heading": "==== knowledge ====", "skip_if_empty": True},
            {"id": "doc", "slot": "doc_context", "heading": "==== doc ====", "skip_if_empty": True},
            {"id": "rev", "slot": "review_program_fail", "heading": "==== program_fail_review ====", "skip_if_empty": True},
            {"id": "img", "kind": "image", "slot": "image_base64", "mime_slot": "image_mime"},
        ],
        "call": {"temperature": 0.15, "max_tokens": 2400, "timeout_sec": 90, "json_mode": True},
        "flags": ["case_execution_use"],
    }
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob

    with session_scope() as db:
        if db.query(LlmJob).filter(LlmJob.id == jid).first():
            return 0
        db.add(_to_row(spec))
        db.flush()
    return 1


AGENT_VISION_ASSERT_V1_MARKER = "agent-vision-assert v1 (batch checkpoints; check phase)"
AGENT_VISION_ASSERT_LANG_V2_MARKER = (
    "agent-vision-assert v2 (expected wording is not a UI language requirement)"
)
_ASSERT_LANG_HINT = """
### 文案语言

预期里出现的中文或英文只是作者的写法，不是要求界面必须使用同一种文字。
按含义、布局、是否出现来判断。含义成立就通过，不要因为界面文字和预期文字不是同一种语言而判失败。
语言是否一致留到以后单独的语言字段，这一步不要做语言检验。
"""
AGENT_VISION_PLAN_PREP_V2_MARKER = "agent-vision-plan v2 (prep program plan; no free milestones)"
_PREP_PROGRAM_PLAN_HINT = """
### 前置 prep（v2）

- `prep_program_plan` 与 `success_criteria.milestones` 已由 **程序从用例密钥 Claim 种子**，顺序固定：筛选账号 → 筛选设备 → 环境清理（按需）→ 打开应用。
- **禁止**输出或改写 `milestones[]`；只允许 `plan_digest`（当前屏观察、风险、下一 hook 参数提示）、`hook_calls`、`flow_block_ops`。
- 登录 UI 步骤不属于 prep 里程碑；勿用 Google/邮箱登录链凑前置。
- 收工由程序按子里程碑聚合，勿 `signal_done`。
"""

AGENT_DECIDE_V23_MARKER = "agent-decide v23 (vision-plan owns milestones when enabled)"

_AGENT_DECIDE_V23_HINT = """
### 里程碑与收工（v23）

当运行环境启用 **agent-vision-plan**（看图规划）时：本 Job **不得**输出 `milestones`、`milestone_updates`、`step_outcome`；不要用 `status=done` / signal_done 收工，程序按子里程碑聚合流转阶段。
里程碑状态一律由程序根据工具结果写回，禁止 `milestone_updates`。
"""


def ensure_agent_vision_assert_job() -> int:
    """P6-P3：从 assert-vision 克隆批量校验 Job（check 阶段）。"""
    import copy

    from mino_nexus.services.job_store import _to_row, _validate_job, get_job

    jid = "agent-vision-assert"
    if get_job(jid):
        return 0
    src = get_job("assert-vision")
    if not src:
        return 0
    spec = copy.deepcopy(src)
    spec["id"] = jid
    spec["label"] = "看图校验"
    spec["summary"] = "check 阶段按里程碑校验点批量断言；失败不翻案"
    spec["prompt_version"] = 1
    spec["builtin"] = True
    spec["enabled"] = True
    blocks = copy.deepcopy(list(spec.get("system_blocks") or []))
    addon = (
        "\n\n### 看图校验（agent-vision-assert）\n"
        "一次调用评估上下文中的**全部校验点 JSON**；输出 passed / confidence / evidence / reasoning。\n"
        "校验失败时程序直接判用例失败，**不接受**翻案为 pending。\n"
        f"({AGENT_VISION_ASSERT_V1_MARKER})\n"
        f"{_ASSERT_LANG_HINT.strip()}\n"
        f"({AGENT_VISION_ASSERT_LANG_V2_MARKER})\n"
    )
    for sb in blocks:
        key = "body" if "body" in sb else "text"
        body = str(sb.get(key) or "")
        if AGENT_VISION_ASSERT_V1_MARKER not in body:
            sb[key] = body.rstrip() + addon
        break
    else:
        blocks.append({"id": "main", "text": addon.strip()})
    spec["system_blocks"] = blocks
    _validate_job(spec)
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.llm_job import LlmJob

    with session_scope() as db:
        if db.query(LlmJob).filter(LlmJob.id == jid).first():
            return 0
        db.add(_to_row(spec))
        db.flush()
    return 1


def upgrade_vision_assert_language_neutral() -> int:
    """预期文案的语言不是界面语种要求。已有 agent-vision-assert / assert-vision 补上这一段。"""
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    changed = 0
    for jid in ("agent-vision-assert", "assert-vision"):
        row = get_job(jid)
        if not row:
            continue
        blocks = list(row.get("system_blocks") or [])
        if any(
            AGENT_VISION_ASSERT_LANG_V2_MARKER in str(spec.get("body") or spec.get("text") or "")
            for spec in blocks
            if isinstance(spec, dict)
        ):
            continue
        merged = copy.deepcopy(row)
        out_blocks = list(merged.get("system_blocks") or [])
        addon = (
            f"\n\n{_ASSERT_LANG_HINT.strip()}\n"
            f"({AGENT_VISION_ASSERT_LANG_V2_MARKER})\n"
        )
        patched = False
        for spec in out_blocks:
            if not isinstance(spec, dict):
                continue
            key = "body" if "body" in spec else "text"
            spec[key] = str(spec.get(key) or "").rstrip() + addon
            patched = True
            break
        if not patched:
            out_blocks.append({"id": "main", "text": addon.strip()})
        merged["system_blocks"] = out_blocks
        revisions = _revision_list(row)
        revisions.append({
            "version": int(row.get("prompt_version") or 1),
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": "before language-neutral vision assert",
            **_blocks_snapshot(row),
        })
        merged["prompt_version"] = int(row.get("prompt_version") or 1) + 1
        _set_revisions(merged, revisions)
        _validate_job(merged)
        _smoke_render(merged)
        _commit_job_upgrade(merged, jid)
        changed += 1
    return changed


def upgrade_agent_vision_plan_to_v2() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-plan"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 6:
        return 0
    blocks = list(row.get("system_blocks") or [])
    for spec in blocks:
        key = "body" if "body" in spec else "text"
        if AGENT_VISION_PLAN_PREP_V2_MARKER in str(spec.get(key) or ""):
            return 0
    merged = copy.deepcopy(row)
    slots = list(merged.get("slots") or [])
    slot_names = {str(s.get("name") or "") for s in slots if isinstance(s, dict)}
    for name in ("prep_program_plan", "resource_claim_json"):
        if name not in slot_names:
            slots.append({"name": name, "kind": "text"})
    merged["slots"] = slots
    blocks = list(merged.get("system_blocks") or [])
    for spec in blocks:
        if str(spec.get("id") or "") != "main":
            continue
        body_key = "body" if "body" in spec else "text"
        body = str(spec.get(body_key) or "")
        if AGENT_VISION_PLAN_PREP_V2_MARKER not in body:
            body = body.rstrip() + "\n\n" + _PREP_PROGRAM_PLAN_HINT.strip() + f"\n\n({AGENT_VISION_PLAN_PREP_V2_MARKER})\n"
        spec[body_key] = body
        break
    user_blocks = list(merged.get("user_blocks") or [])
    ub_ids = {str(u.get("id") or "") for u in user_blocks if isinstance(u, dict)}
    if "prep_plan" not in ub_ids:
        user_blocks.insert(
            3,
            {
                "id": "prep_plan",
                "slot": "prep_program_plan",
                "heading": "==== prep_program_plan（只读） ====",
                "skip_if_empty": True,
            },
        )
    if "claim" not in ub_ids:
        user_blocks.insert(
            4,
            {
                "id": "claim",
                "slot": "resource_claim_json",
                "heading": "==== resource_claim（只读） ====",
                "skip_if_empty": True,
            },
        )
    merged["user_blocks"] = user_blocks
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "before program upgrade to v2 prep program plan slots",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = max(int(row.get("prompt_version") or 1), 2)
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


AGENT_VISION_PLAN_DO_CHECK_V3_MARKER = "agent-vision-plan v3 (do/check program plan; check isolated from do digest)"
_DO_CHECK_PROGRAM_HINT = """
### do / check（v3）

- **do**：`do_program_plan` 与子里程碑已由程序从 **操作层密钥** 种子；禁止改写 `milestones[]` 顺序与 id，只输出 `plan_digest`、`flow_block_ops`、`hook_calls`。
- **check**：只读 `check_program_plan` 与 **expected** 校验点；只输出 `checkpoints_plan`（对齐已有 checkpoint id）与 `plan_digest`（观察摘要）。**禁止**引用 do 阶段 `plan_digest` 或操作里程碑。
- 一行操作可对应多步：以 `do_program_plan.steps` 为准，勿合并为单步。
- 无密钥匹配的行已在 `key_compile/fallback` 标红；勿用自由 milestones 替代 catalog 骨架。
"""


def upgrade_agent_vision_plan_to_v3() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-plan"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 6:
        return 0
    blocks = list(row.get("system_blocks") or [])
    for spec in blocks:
        key = "body" if "body" in spec else "text"
        if AGENT_VISION_PLAN_DO_CHECK_V3_MARKER in str(spec.get(key) or ""):
            return 0
    merged = copy.deepcopy(row)
    slots = list(merged.get("slots") or [])
    slot_names = {str(s.get("name") or "") for s in slots if isinstance(s, dict)}
    for name in ("do_program_plan", "check_program_plan"):
        if name not in slot_names:
            slots.append({"name": name, "kind": "text"})
    merged["slots"] = slots
    blocks = list(merged.get("system_blocks") or [])
    for spec in blocks:
        if str(spec.get("id") or "") != "main":
            continue
        body_key = "body" if "body" in spec else "text"
        body = str(spec.get(body_key) or "")
        if AGENT_VISION_PLAN_DO_CHECK_V3_MARKER not in body:
            body = body.rstrip() + "\n\n" + _DO_CHECK_PROGRAM_HINT.strip() + f"\n\n({AGENT_VISION_PLAN_DO_CHECK_V3_MARKER})\n"
        spec[body_key] = body
        break
    user_blocks = list(merged.get("user_blocks") or [])
    ub_ids = {str(u.get("id") or "") for u in user_blocks if isinstance(u, dict)}
    if "do_plan" not in ub_ids:
        user_blocks.append(
            {
                "id": "do_plan",
                "slot": "do_program_plan",
                "heading": "==== do_program_plan（只读） ====",
                "skip_if_empty": True,
            },
        )
    if "check_plan" not in ub_ids:
        user_blocks.append(
            {
                "id": "check_plan",
                "slot": "check_program_plan",
                "heading": "==== check_program_plan（只读） ====",
                "skip_if_empty": True,
            },
        )
    merged["user_blocks"] = user_blocks
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "before program upgrade to v3 do/check program plan",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = max(int(row.get("prompt_version") or 1), 3)
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


AGENT_VISION_PLAN_V4_MARKER = "agent-vision-plan v4 (success_criteria only; task+case context)"
AGENT_VISION_EXEC_V2_MARKER = "agent-vision-exec v2 (standalone executor; no decide appendix)"

_VISION_PLAN_V4_SYSTEM = """\
你是测试执行**规划器**（agent-vision-plan）。根据截图与 JSON 上下文，为本回合写观察摘要与可执行提示。

阶段 phase={{phase}}

铁律：
- **进度真源**只有 `success_criteria`（含程序种子的 milestones）与各 phase 的 `*_program_plan`；不要依赖 history/checkpoints。
- **禁止**输出 `milestones[]`、`milestone_updates`、`step_outcome`；里程碑 pass/fail 由程序根据工具结果写回。
- prep/do：可输出 `plan_digest`、`hook_calls`、`flow_block_ops`（参数提示，不是设备 tap）。
- check：只输出 `checkpoints_plan` 与 `plan_digest`；不得引用 do 阶段 plan_digest。
- 若 `review_program_fail` 非空：输出 `failure_verdict`。
- 设备渠道、租号结果由 Nexus 处理，不要在规划里编排 pick_device / 账号明细。

只输出 JSON：
{"thought":"","flow_block_ops":[],"hook_calls":[],"checkpoints_plan":[],"plan_digest":{},"failure_verdict":{}}
"""

_VISION_PLAN_V4_USER_BLOCKS = [
    {"id": "task", "slot": "task_context_json", "heading": "==== task_context ===="},
    {"id": "case", "slot": "case_execution_context_json", "heading": "==== case_execution ===="},
    {"id": "phase_txt", "slot": "phase_step_text", "heading": "==== phase_step_text ===="},
    {"id": "sc", "slot": "success_criteria", "heading": "==== success_criteria ===="},
    {
        "id": "prep_plan",
        "slot": "prep_program_plan",
        "heading": "==== prep_program_plan（只读） ====",
        "skip_if_empty": True,
    },
    {
        "id": "do_plan",
        "slot": "do_program_plan",
        "heading": "==== do_program_plan（只读） ====",
        "skip_if_empty": True,
    },
    {
        "id": "check_plan",
        "slot": "check_program_plan",
        "heading": "==== check_program_plan（只读） ====",
        "skip_if_empty": True,
    },
    {"id": "sess", "slot": "session_json", "heading": "==== session（登录态摘要） ===="},
    {"id": "nav", "slot": "nav_assist", "heading": "==== nav ====", "skip_if_empty": True},
    {"id": "know", "slot": "knowledge_body", "heading": "==== knowledge ====", "skip_if_empty": True},
    {"id": "doc", "slot": "doc_context", "heading": "==== doc ====", "skip_if_empty": True},
    {
        "id": "rev",
        "slot": "review_program_fail",
        "heading": "==== program_fail_review ====",
        "skip_if_empty": True,
    },
    {"id": "img", "kind": "image", "slot": "image_base64", "mime_slot": "image_mime"},
]

_VISION_EXEC_V2_SYSTEM = """\
你是测试**看图执行器**（agent-vision-exec）。根据 plan_digest、active_milestone、success_criteria 与截图，**调用一个** function tool 完成当前 pending 里程碑对应的一步操作。

铁律：
- 只输出 tool call，不要正文 JSON。
- 工具参数里只需简短 `thought`；**不要** milestone_updates / step_outcome / remember 长列表。
- **禁止** signal_done、signal_give_up、signal_ask_human、signal_skip（阶段收工由程序聚合里程碑）。
- 需要账号/OTP/手机号时调用 lease_account、get_otp 等能力；账号句柄已在运行上下文，不要向用户复述明文。
- 前置 read_device_data 等边界以 catalog 能力说明为准。

每回合最多一步设备操作，然后交给程序写回里程碑。
"""

_VISION_EXEC_V2_USER_BLOCKS = [
    {"id": "task", "slot": "task_context_json", "heading": "==== task_context ===="},
    {"id": "case", "slot": "case_execution_context_json", "heading": "==== case_execution ===="},
    {"id": "plan", "slot": "plan_digest", "heading": "==== plan_digest ===="},
    {"id": "am", "slot": "active_milestone", "heading": "==== active_milestone ===="},
    {"id": "sc", "slot": "success_criteria", "heading": "==== success_criteria ===="},
    {"id": "goal", "slot": "goal", "heading": "==== goal ===="},
    {"id": "sess", "slot": "session_json", "heading": "==== session ===="},
    {"id": "nav", "slot": "nav_assist", "heading": "==== nav ====", "skip_if_empty": True},
    {"id": "know", "slot": "knowledge_body", "heading": "==== knowledge ====", "skip_if_empty": True},
    {"id": "doc", "slot": "doc_context", "heading": "==== doc ====", "skip_if_empty": True},
    {"id": "hier", "slot": "hierarchy_text", "heading": "==== hierarchy ====", "skip_if_empty": True},
    {"id": "img", "kind": "image", "slot": "image_base64", "mime_slot": "image_mime"},
]


def upgrade_agent_vision_plan_to_v4() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-plan"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 6:
        return 0
    for spec in row.get("system_blocks") or []:
        key = "body" if "body" in spec else "text"
        if AGENT_VISION_PLAN_V4_MARKER in str(spec.get(key) or ""):
            return 0
    merged = copy.deepcopy(row)
    slot_names = {
        "phase",
        "phase_step_text",
        "success_criteria",
        "target_app",
        "session_json",
        "screen_size_block",
        "hierarchy_text",
        "nav_assist",
        "knowledge_hint",
        "knowledge_body",
        "doc_context",
        "review_program_fail",
        "prep_program_plan",
        "do_program_plan",
        "check_program_plan",
        "task_context_json",
        "case_execution_context_json",
        "image_base64",
        "image_mime",
    }
    merged["slots"] = [{"name": n, "kind": "text" if n != "image_base64" else "image"} for n in sorted(slot_names)]
    merged["slots"] = [
        {"name": "image_base64", "kind": "image"},
        {"name": "image_mime", "kind": "text"},
    ] + [{"name": n, "kind": "text"} for n in sorted(slot_names - {"image_base64", "image_mime"})]
    merged["system_blocks"] = [
        {
            "id": "main",
            "text": _VISION_PLAN_V4_SYSTEM.strip() + f"\n\n({AGENT_VISION_PLAN_V4_MARKER})\n",
        }
    ]
    merged["user_blocks"] = list(_VISION_PLAN_V4_USER_BLOCKS)
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "before v4 success_criteria-only context",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = max(int(row.get("prompt_version") or 1), 4)
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


def upgrade_agent_vision_exec_to_v2() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-exec"
    row = get_job(jid)
    if not row:
        return 0
    for spec in row.get("system_blocks") or []:
        key = "body" if "body" in spec else "text"
        if AGENT_VISION_EXEC_V2_MARKER in str(spec.get(key) or ""):
            return 0
    merged = copy.deepcopy(row)
    exec_slots = {
        "goal",
        "plan_digest",
        "active_milestone",
        "success_criteria",
        "target_app",
        "phase",
        "session_json",
        "task_context_json",
        "case_execution_context_json",
        "screen_size_block",
        "knowledge_hint",
        "knowledge_body",
        "hierarchy_text",
        "nav_assist",
        "doc_context",
        "menu_json",
        "image_base64",
        "image_mime",
    }
    merged["slots"] = [
        {"name": "image_base64", "kind": "image"},
        {"name": "image_mime", "kind": "text"},
    ] + [{"name": n, "kind": "text"} for n in sorted(exec_slots - {"image_base64", "image_mime"})]
    merged["system_blocks"] = [
        {
            "id": "main",
            "text": _VISION_EXEC_V2_SYSTEM.strip() + f"\n\n({AGENT_VISION_EXEC_V2_MARKER})\n",
        }
    ]
    merged["user_blocks"] = list(_VISION_EXEC_V2_USER_BLOCKS)
    merged["summary"] = "按 plan_digest + success_criteria 单步执行 capability（无 milestone_updates）"
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "before v2 standalone executor prompt",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = max(int(row.get("prompt_version") or 1), 2)
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


_VISION_PLAN_V5_SYSTEM = """\
你是测试执行规划器。输入为 success_criteria（含 milestones 列表：id、title、kind、status、hook_cap 等）和当前截图；可选知识、导航、文档辅助。

你的职责：根据画面判断本阶段还需哪些动作，维护 milestones 列表。执行器会按列表顺序调用设备能力，不由你直接点击。

输出 JSON：
{"thought":"简短观察与理由","milestones_append":[{"id":"新id","title":"动作描述","kind":"hook|visual_action|checkpoint","hook_cap":"能力名如 launch_app","optional":false}],"step_requirements_complete":false,"failure_verdict":{}}

规则：
- 不得删除或重排已有 id；不要输出 milestone_updates（状态由程序写回）。
- 列表不足以完成本阶段时，用 milestones_append 在末尾追加。
- 不要输出 hook_calls、flow_block_ops、plan_digest、history、账号或设备派单信息。
- 若附 program_fail_review，可填 failure_verdict.blocking 与 reason。
"""

_VISION_PLAN_V5_USER_BLOCKS = [
    {"id": "sc", "slot": "success_criteria", "heading": "success_criteria"},
    {"id": "know", "slot": "knowledge_body", "heading": "knowledge", "skip_if_empty": True},
    {"id": "nav", "slot": "nav_assist", "heading": "nav", "skip_if_empty": True},
    {"id": "doc", "slot": "doc_context", "heading": "doc", "skip_if_empty": True},
    {"id": "hier", "slot": "hierarchy_text", "heading": "hierarchy", "skip_if_empty": True},
    {
        "id": "rev",
        "slot": "review_program_fail",
        "heading": "program_fail_review",
        "skip_if_empty": True,
    },
    {"id": "img", "kind": "image", "slot": "image_base64", "mime_slot": "image_mime"},
]

_VISION_EXEC_V3_SYSTEM = """\
你是测试执行器。输入为 success_criteria（milestones 含每条 status：pending、in_progress、pass、failed、skipped）和截图。

对列表中第一条 status 为 pending 或 in_progress 的里程碑，调用一个 function tool 完成对应一步。pass 或 failed 的不要重复执行。

账号、OTP、设备参数由系统自动注入；需要时调用 lease_account、get_otp 等能力，不要向用户复述账号明文。

只输出 tool call，不要 JSON 正文。不要 signal_done。
"""

_VISION_EXEC_V3_USER_BLOCKS = [
    {"id": "sc", "slot": "success_criteria", "heading": "success_criteria"},
    {"id": "nav", "slot": "nav_assist", "heading": "nav", "skip_if_empty": True},
    {"id": "know", "slot": "knowledge_body", "heading": "knowledge", "skip_if_empty": True},
    {"id": "doc", "slot": "doc_context", "heading": "doc", "skip_if_empty": True},
    {"id": "hier", "slot": "hierarchy_text", "heading": "hierarchy", "skip_if_empty": True},
    {"id": "img", "kind": "image", "slot": "image_base64", "mime_slot": "image_mime"},
]


_VISION_PLAN_V6_SYSTEM = """\
你是测试执行规划器。输入为 success_criteria（milestones：id、title、kind、status、hook_cap）和截图；可选知识、导航、文档。

职责：只追加里程碑、判断本步是否已满足要求。每条里程碑的 pass/failed/skipped 由程序根据工具执行结果写回，你不要改已有条的 status。

输出 JSON：
{"thought":"…","milestones_append":[{"id":"…","title":"…","kind":"hook|visual_action|checkpoint","hook_cap":"…","optional":false}],"exit_allowed":false,"failure_verdict":{}}

规则：
- 不得删除或重排已有 id；不要输出 milestone_updates。
- 列表不足以完成本 phase/本步时，用 milestones_append 在末尾追加；追加时 thought 须写清：虽然用例表面只需 …，但是不做 … 就无法完成 …。
- exit_allowed 是准出，默认 false。每一轮都判断要不要改它：若执行完当前列表的剩余步骤就能离开本 phase/本步，设为 true，且不要再 append。false 表示本轮不改准出。
- 程序只在最后一条里程碑终态之后才读 exit_allowed。列表中途即便为 true，剩余步骤仍会执行，不会提前跳步。
- hook_cap 只能从 success_criteria.allowed_capability_ids 里选。页面是否加载完用 wait_screen_ready。不要发明目录里没有的能力名。
- 不要输出 hook_calls、plan_digest、history、账号或设备派单字段。
"""

_VISION_EXEC_V4_SYSTEM = """\
你是测试执行器。输入 success_criteria（含 active_focus_milestone_id 与每条 status）和截图。

只处理 status 为 in_progress 的那一条里程碑（与 active_focus_milestone_id 一致）；对其调用一个 function tool。pass/failed/skipped 的不得重复执行；pending 的由程序切换为 in_progress 后再由你执行。

账号、OTP 由系统注入；需要时调用 lease_account、get_otp。只输出 tool call，不要 signal_done。
"""


def upgrade_agent_vision_plan_to_v7() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-plan"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 7:
        return 0
    merged = copy.deepcopy(row)
    merged["slots"] = list(row.get("slots") or merged.get("slots") or [])
    merged["system_blocks"] = [{"id": "main", "text": _VISION_PLAN_V6_SYSTEM.strip()}]
    merged["user_blocks"] = list(_VISION_PLAN_V5_USER_BLOCKS)
    merged["summary"] = "仅 milestones_append；状态由程序写回"
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v7 no milestone_updates on plan",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 7
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


def upgrade_agent_vision_plan_to_v8() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-plan"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 8:
        return 0
    merged = copy.deepcopy(row)
    merged["slots"] = list(row.get("slots") or merged.get("slots") or [])
    merged["system_blocks"] = [{"id": "main", "text": _VISION_PLAN_V6_SYSTEM.strip()}]
    merged["user_blocks"] = list(_VISION_PLAN_V5_USER_BLOCKS)
    merged["summary"] = "准出 exit_allowed；状态由程序写回"
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v8 exit_allowed latch",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 8
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


def upgrade_agent_vision_plan_to_v9() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-plan"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 9:
        return 0
    merged = copy.deepcopy(row)
    merged["slots"] = list(row.get("slots") or merged.get("slots") or [])
    merged["system_blocks"] = [{"id": "main", "text": _VISION_PLAN_V6_SYSTEM.strip()}]
    merged["user_blocks"] = list(_VISION_PLAN_V5_USER_BLOCKS)
    merged["summary"] = "hook_cap 必须来自本阶段菜单"
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v9 hook_cap must be registered",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 9
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


_VISION_PLAN_V11_SYSTEM = """\
你是测试执行规划器。输入为 success_criteria（milestones：id、title、kind、status、hook_cap）和截图；可选知识、导航、文档。

职责：只追加本阶段正文里还没有的里程碑，并判断能否离开本阶段。每条里程碑的 pass/failed/skipped 由程序根据工具结果写回，你不要改已有条的 status。

输出 JSON：
{"thought":"…","milestones_append":[{"id":"…","title":"…","kind":"hook|visual_action|checkpoint","hook_cap":"…","optional":false}],"exit_allowed":false,"failure_verdict":{}}

规则：
- 不得删除或重排已有 id；不要输出 milestone_updates。
- 已有里程碑已经覆盖本阶段要求时，milestones_append 必须为空。全部非 optional 条目已是 pass、failed 或 skipped 时，exit_allowed 设为 true，不要再追加。
- 只有本阶段正文写了、列表里还没有对应条目时才追加。不要因为画面上有分类、卡片或按钮，就追加进入后续测试阶段的点击。前置阶段不要追加操作步骤或校验步骤的点击。
- 逻辑块里的点击已经在里程碑列表里，由程序按条执行。不要在这些条目之外再追加一次点击。
- exit_allowed 默认 false。执行完当前列表的剩余步骤就能离开本阶段时设为 true，且不要再 append。false 表示本轮不改准出。
- 程序只在最后一条里程碑终态之后才读 exit_allowed。列表中途即便为 true，剩余步骤仍会执行，不会提前跳步。
- hook_cap 只能从 success_criteria.allowed_capability_ids 里选。页面是否加载完用 wait_screen_ready。不要发明目录里没有的能力名。
- thought 只说明本阶段还缺什么，或为什么可以离开。不要写「虽然用例表面只需…但是不做…就无法完成…」，不要用这个句式编造额外步骤。
- 不要输出 hook_calls、plan_digest、history、账号或设备派单字段。
"""


def upgrade_agent_vision_plan_to_v11() -> int:
    """去掉强迫模型用「虽然…但是…」编造额外点击的句子。"""
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-plan"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 11:
        return 0
    merged = copy.deepcopy(row)
    merged["slots"] = list(row.get("slots") or merged.get("slots") or [])
    merged["system_blocks"] = [{"id": "main", "text": _VISION_PLAN_V11_SYSTEM.strip()}]
    merged["user_blocks"] = list(_VISION_PLAN_V5_USER_BLOCKS)
    merged["summary"] = "列表已覆盖时准出，不编造后续点击"
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v11 do not invent clicks after program list is done",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 11
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


def upgrade_agent_vision_plan_to_v10() -> int:
    """把准出提示写回。v4 升级曾在版本号已经是 9 时把正文盖回旧模板。"""
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-plan"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 10:
        return 0
    merged = copy.deepcopy(row)
    merged["slots"] = list(row.get("slots") or merged.get("slots") or [])
    merged["system_blocks"] = [{"id": "main", "text": _VISION_PLAN_V6_SYSTEM.strip()}]
    merged["user_blocks"] = list(_VISION_PLAN_V5_USER_BLOCKS)
    merged["summary"] = "准出 exit_allowed；hook_cap 必须来自本阶段菜单"
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v10 restore exit_allowed after v4 clobber",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 10
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


def upgrade_agent_vision_plan_to_v6() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-plan"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 6:
        return 0
    merged = copy.deepcopy(row)
    merged["slots"] = list(row.get("slots") or merged.get("slots") or [])
    merged["system_blocks"] = [{"id": "main", "text": _VISION_PLAN_V6_SYSTEM.strip()}]
    merged["user_blocks"] = list(_VISION_PLAN_V5_USER_BLOCKS)
    merged["summary"] = "里程碑状态机：追加/更新/step_requirements_complete"
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v6 plan-exec FSM",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 6
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


def upgrade_agent_vision_exec_to_v4() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-exec"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 4:
        return 0
    merged = copy.deepcopy(row)
    merged["slots"] = list(row.get("slots") or merged.get("slots") or [])
    merged["system_blocks"] = [{"id": "main", "text": _VISION_EXEC_V4_SYSTEM.strip()}]
    merged["user_blocks"] = list(_VISION_EXEC_V3_USER_BLOCKS)
    merged["summary"] = "仅执行 in_progress 里程碑（active_focus）"
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v4 in_progress focus",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 4
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


def upgrade_agent_vision_plan_to_v5() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-plan"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 5:
        return 0
    merged = copy.deepcopy(row)
    merged["slots"] = [
        {"name": "success_criteria", "kind": "text"},
        {"name": "knowledge_hint", "kind": "text"},
        {"name": "knowledge_body", "kind": "text"},
        {"name": "nav_assist", "kind": "text"},
        {"name": "doc_context", "kind": "text"},
        {"name": "hierarchy_text", "kind": "text"},
        {"name": "review_program_fail", "kind": "text"},
        {"name": "image_base64", "kind": "image"},
        {"name": "image_mime", "kind": "text"},
    ]
    merged["system_blocks"] = [{"id": "main", "text": _VISION_PLAN_V5_SYSTEM.strip()}]
    merged["user_blocks"] = list(_VISION_PLAN_V5_USER_BLOCKS)
    merged["summary"] = "维护 success_criteria.milestones；不注入运行上下文"
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v5 milestones-only LLM injection",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 5
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


def upgrade_agent_vision_exec_to_v3() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    jid = "agent-vision-exec"
    row = get_job(jid)
    if not row or int(row.get("prompt_version") or 0) >= 3:
        return 0
    merged = copy.deepcopy(row)
    merged["slots"] = [
        {"name": "success_criteria", "kind": "text"},
        {"name": "knowledge_hint", "kind": "text"},
        {"name": "knowledge_body", "kind": "text"},
        {"name": "nav_assist", "kind": "text"},
        {"name": "doc_context", "kind": "text"},
        {"name": "hierarchy_text", "kind": "text"},
        {"name": "menu_json", "kind": "text"},
        {"name": "image_base64", "kind": "image"},
        {"name": "image_mime", "kind": "text"},
    ]
    merged["system_blocks"] = [{"id": "main", "text": _VISION_EXEC_V3_SYSTEM.strip()}]
    merged["user_blocks"] = list(_VISION_EXEC_V3_USER_BLOCKS)
    merged["summary"] = "按 success_criteria.milestones 单步执行 capability"
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v3 milestones-only LLM injection",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = 3
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, jid)
    return 1


def upgrade_agent_decide_to_v23() -> int:
    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    blocks = list(row.get("system_blocks") or [])
    for spec in blocks:
        key = "body" if "body" in spec else "text"
        if AGENT_DECIDE_V23_MARKER in str(spec.get(key) or ""):
            return 0
    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    for spec in blocks:
        name = str(spec.get("name") or "")
        if name not in ("system", ""):
            continue
        body_key = "body" if "body" in spec else "text"
        body = str(spec.get(body_key) or "")
        if AGENT_DECIDE_V23_MARKER not in body:
            body = body.rstrip() + "\n\n" + _AGENT_DECIDE_V23_HINT.strip() + f"\n\n({AGENT_DECIDE_V23_MARKER})\n"
        spec[body_key] = body
        break
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "before program upgrade to v23 vision-plan milestone split",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = max(int(row.get("prompt_version") or 1), 23)
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1


AGENT_DECIDE_V24_MARKER = "agent-decide v24 (no milestone_updates; program writes status)"

_AGENT_DECIDE_V24_HINT = """
### 里程碑状态
- **禁止**在 JSON 或 tool 参数中输出 `milestone_updates`。
- `success_criteria.milestones` 的 pass/failed/skipped 仅由服务端根据工具执行结果写回。
"""


def upgrade_agent_decide_to_v24() -> int:
    import copy
    from datetime import datetime

    from mino_nexus.services.job_store import (
        _blocks_snapshot,
        _revision_list,
        _set_revisions,
        _smoke_render,
        _validate_job,
        get_job,
    )

    row = get_job("agent-decide")
    if not row:
        return 0
    blocks = list(row.get("system_blocks") or [])
    for spec in blocks:
        key = "body" if "body" in spec else "text"
        if AGENT_DECIDE_V24_MARKER in str(spec.get(key) or ""):
            return 0
    merged = copy.deepcopy(row)
    blocks = list(merged.get("system_blocks") or [])
    for spec in blocks:
        name = str(spec.get("name") or "")
        if name not in ("system", ""):
            continue
        body_key = "body" if "body" in spec else "text"
        body = str(spec.get(body_key) or "")
        if AGENT_DECIDE_V24_MARKER not in body:
            body = body.rstrip() + "\n\n" + _AGENT_DECIDE_V24_HINT.strip() + f"\n\n({AGENT_DECIDE_V24_MARKER})\n"
        spec[body_key] = body
        break
    merged["system_blocks"] = blocks
    revisions = _revision_list(row)
    revisions.append({
        "version": int(row.get("prompt_version") or 1),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "v24 remove milestone_updates",
        **_blocks_snapshot(row),
    })
    merged["prompt_version"] = max(int(row.get("prompt_version") or 1), 24)
    _set_revisions(merged, revisions)
    _validate_job(merged)
    _smoke_render(merged)
    _commit_job_upgrade(merged, "agent-decide")
    return 1

