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
