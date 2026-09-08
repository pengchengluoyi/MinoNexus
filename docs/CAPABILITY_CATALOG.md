# CAPABILITY_CATALOG — 能力目录

能力目录是 **Nexus 独有**的资产，Scout 不读它。目录声明"能做什么、用什么实现"，**不写"什么时候做、按什么顺序做"** —— 后者是 LLM 的事。

运行时真源是 `mino.db` 的 `catalog_entries`（`kind` = `abstract_cap` / `executor` / `capability` / `recovery` / `resource`）。空库由 `catalog/builtin_seed.py` 灌一次，之后改库，升级不自动覆盖已有行。管理员可经 `/packs` 写 builtin 行，不回写任何文件。

## 1. 三层抽象

```
abstract_cap                抽象能力全集（system_shell / ui_native_input / ...）
        ▲ provides
executor                    每个执行器声明自己实现了哪些抽象能力
        ▲ requires_caps
capability                  每个能力声明它需要哪些抽象能力 + 有哪几条实现路径
```

这一层间接是拆分能成立的原因：**capability 声明的是抽象 cap，不是具体的 `adb` / `remote`。** 加一种执行通道只需加一个 executor 行 + Scout 侧一个实现类，已有能力自动获得新路径。

## 2. 表里的 kind

| kind | 内容 |
|---|---|
| `abstract_cap` | 抽象能力全集 |
| `executor` | adb / remote / ios_wda / playwright / ai_persona / vlm / hitl |
| `capability` | 每个 event_kind 一行 |
| `recovery` | L0 恢复规则 |
| `resource` | 号池 / OTP / session 等资源能力 |

`lifecycle=deprecated` 且 `enabled=false` 是软删除。空库种子在 `mino_nexus/catalog/builtin_seed.py`。

## 3. Capability 字段

```
id: tap_element
display_name: 点击元素
event_kind: tap_element
category: ui_interaction
platforms: [android, ios, web]
needs_vlm: true
implementations:                 # 按 cost 升序
  - id / executor / requires_caps / needs_vlm / low_level / cost
```

`low_level` 段会**原样下发给 Scout**（`EXECUTE.low_level`），由 Scout 的 `low_level.py` 填充占位符并执行。

## 4. Executor 字段

```
id: adb
available_when: connectivity.adb        # 由 Scout 上报的连通性决定
provides: [system_shell, system_pkg_clear, ui_native_input, ...]
platforms: [android]
conditional_provides:                   # 视设备运行时权限而定
  - cap: system_pkg_clear
    requires_remote_caps: [device_owner]
```

**`available_when` 的数据来自 Scout 的 `REGISTER` / `HEARTBEAT`，不是 Nexus 自己探的。**

## 5. 菜单怎么产生

```
Scout REGISTER: executors[].provides（abstract cap 字符串）
   ∩ capabilities[].implementations[].requires_caps
   → filter_capabilities_by_connectivity()
   → tool_schema：capability → OpenAI function tools
   → planner.decide_next_action 的可选动作集
```

不同连通性下的瘦身规则：

| connectivity | 行为 |
|---|---|
| adb ✓ remote ✓ | 完整菜单 |
| adb ✓ remote ✗ | 砍 remote 独有实现 |
| adb ✗ remote ✓ | 砍 adb 独有实现；清缓存自动落到 `ai_persona` 拟人路径 |
| 只有 playwright | 网页菜单，tap/input 不先 VLM locate |
| 全无 | **空菜单，应 decline** —— 不要下发 |

## 6. 加一条能力

大多数情况**只改 `catalog_entries`，Scout 侧零改动**：

1. 插入或更新一行 `kind=capability`（Console 扩展包，或改 `builtin_seed` 后空库重灌）
2. 引入了新抽象 cap → 加 `kind=abstract_cap`，并在相关 executor 的 `provides` 补上
3. 用 `low_level` 表达执行形式（`shell` 或 `command`）
4. `POST /settings/skills/reload`（或 `POST /packs/reload`）
5. Skills / 扩展包页刷新 → 生效

**只有引入新原语时才需要动 Scout 的 Python**（现有 `shell` / `command` 表达不了的动作）。判据：能不能用 `expands_to_events` 拆成已有原语的组合？能就不要动代码。

## 7. 加一个执行器

1. 加 `kind=executor` 行，声明 `provides`
2. **Scout 侧**实现对应 executor 类 + 连通性探测 + 加进 `manifest()`（见 `../MinoScout/docs/EXECUTORS.md` §4）
3. 已有 capability 补一条 `implementations` 指向新 executor，`cost` 低于 VLM 路径的优先

**两仓都要改。** 只加目录行会让 Nexus 把动作派给一个 Scout 不认识的 executor；只改 Scout 则 Nexus 永远不会选它。

## 8. 反模式

| 不要 | 为什么 |
|---|---|
| 在目录里写执行顺序 / 条件判断 | 顺序是 LLM 决定的。目录只声明能力 |
| capability 直接绑 `executor: adb` 而不声明 `requires_caps` | 破坏抽象层，加通道时要改所有能力 |
| 在 `adb_executor.py` 里写 `if capability_id == ...` 长链 | 用 `low_level`。这是上游被明确批评过的反模式 |
| 再给 Scout 一份目录副本 | 必然漂移 |

## 9. Recovery 规则（Console 扩展包 → kind=recovery）

跑批开环前与 `wait_screen_ready` 会读 `catalog_entries` 里 `kind=recovery` 的行。Console **扩展包**页可新建/编辑（`POST /packs`），不必改代码。

`payload.match` 支持：

| 字段 | 含义 |
|---|---|
| `evidence` | 全部 key 同时满足（AND） |
| `evidence_any` | 任一分支满足（OR），每分支为 `{capture_black: yes}` 等 |
| `screen_text_any` | 截图 OCR 文本命中（后续） |

常用取证 key（`probe_device_state` + 截图分析）：

| key | yes 表示 |
|---|---|
| `screen_blocked` | 休眠或 keyguard |
| `capture_black` | 截图均值极暗（**锁屏时常只有黑图**） |
| `capture_ok` | Scout 返回可解码截图 |

内置 `screen_asleep_or_locked`：`evidence_any` 三者任一命中 → `wake_screen` + `dismiss_keyguard`；verify 要求三项均正常。

Agent 在 prep/do 菜单里也可主动调 `recover_<规则id>`（prep 阶段已含 `recovery` tool_kind）。

