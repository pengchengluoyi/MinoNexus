# CAPABILITY_CATALOG — 能力目录

能力目录是 **Nexus 独有**的资产，Scout 不读它。目录声明"能做什么、用什么实现"，**不写"什么时候做、按什么顺序做"** —— 后者是 LLM 的事。

## 1. 三层抽象

```
abstract_caps.yaml          抽象能力全集（system_shell / ui_native_input / ...）
        ▲ provides
executors/*.yaml            每个执行器声明自己实现了哪些抽象能力
        ▲ requires_caps
capabilities/*.yaml         每个能力声明它需要哪些抽象能力 + 有哪几条实现路径
```

这一层间接是拆分能成立的原因：**capability 声明的是抽象 cap，不是具体的 `adb` / `remote`。** 加一种执行通道只需加一个 executor YAML + Scout 侧一个实现类，已有能力自动获得新路径。

## 2. 目录结构

```
mino_nexus/catalog/plugins/
├── abstract_caps.yaml
├── executors/          adb / remote / ios_wda / playwright / ai_persona / vlm / hitl
├── capabilities/       每个 event_kind 一个 yaml
├── recovery/           L0 恢复规则
└── resources/          号池 / OTP / session 等资源能力
```

`*.yaml.disabled` 被加载器跳过，用作未来执行器占位。

## 3. Capability YAML

```yaml
id: tap_element
display_name: 点击元素
event_kind: tap_element
category: ui_interaction
description: 点击屏幕上的指定元素，VLM 给出坐标
platforms: [android, ios, web]
trigger_phrases: [点击, 点, 按, tap, click]
needs_vlm: true                  # 默认值，可被单条 implementation 覆盖

implementations:                 # 按 cost 升序
  - id: playwright_tap
    executor: playwright
    requires_caps: [ui_native_input, ui_screenshot]
    needs_vlm: false             # 按名字点，不先 VLM locate
    cost: 3
  - id: vlm_locate_adb_tap
    executor: adb
    requires_caps: [ui_native_input, ui_screenshot]
    needs_vlm: true
    locate_prompt: LOCATE_VISION
    low_level:
      shell: "input tap {x} {y}"
    cost: 4
  - id: vlm_locate_remote_tap
    executor: remote
    requires_caps: [ui_native_input, ui_screenshot]
    needs_vlm: true
    locate_prompt: LOCATE_VISION
    low_level:
      command: TAP
      params: { x: "{x}", y: "{y}" }
    cost: 5

ui:
  shown_in_settings: true
  examples: ["点击「同意」"]
```

`low_level` 段会**原样下发给 Scout**（`EXECUTE.low_level`），由 Scout 的 `low_level.py` 填充占位符并执行。

## 4. Executor YAML

```yaml
id: adb
display_name: ADB
description: 通过 USB / TCP adb 直连，最特权
available_when: connectivity.adb        # 由 Scout 上报的连通性决定
provides:
  - system_shell
  - system_pkg_clear
  - ui_native_input
  - ...
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

大多数情况**只改 YAML，Scout 侧零改动**：

1. `capabilities/` 加一个 yaml
2. 引入了新抽象 cap → 在 `abstract_caps.yaml` 登记，并在相关 `executors/*.yaml` 的 `provides` 补上
3. 用 `low_level` 表达执行形式（`shell` 或 `command`）
4. `POST /settings/skills/reload`（或等 mtime 检测）
5. Skills 页刷新 → 生效

**只有引入新原语时才需要动 Scout 的 Python**（现有 `shell` / `command` 表达不了的动作）。判据：能不能用 `expands_to_events` 拆成已有原语的组合？能就不要动代码。

## 7. 加一个执行器

1. `executors/` 加 yaml，声明 `provides`
2. **Scout 侧**实现对应 executor 类 + 连通性探测 + 加进 `manifest()`（见 `../MinoScout/docs/EXECUTORS.md` §4）
3. 已有 capability 补一条 `implementations` 指向新 executor，`cost` 低于 VLM 路径的优先

**两仓都要改。** 只加 YAML 会让 Nexus 把动作派给一个 Scout 不认识的 executor；只改 Scout 则 Nexus 永远不会选它。

## 8. 反模式

| 不要 | 为什么 |
|---|---|
| 在 YAML 里写执行顺序 / 条件判断 | 顺序是 LLM 决定的。YAML 只声明能力 |
| capability 直接绑 `executor: adb` 而不声明 `requires_caps` | 破坏抽象层，加通道时要改所有能力 |
| 在 `adb_executor.py` 里写 `if capability_id == ...` 长链 | 用 `low_level`。这是上游被明确批评过的反模式 |
| 把 YAML 复制到 Scout | 必然漂移。`verify_no_yaml_catalog.py` 会拦 |
