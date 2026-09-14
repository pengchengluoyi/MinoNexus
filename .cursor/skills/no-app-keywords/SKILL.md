---
name: no-app-keywords
description: >-
  禁止在导航/合成/布局代码里硬编码被测 App 的 UI 文案或 Tab 关键字白名单。
  写或改 nav_*、synthesis、localize、layout 相关代码时必须遵守；发现违例立即修复。
---

# 禁止 App 文案硬编码

## 铁律

**永远不要**在业务代码里写针对某个 App 的 UI 文案、Tab 名、页面角色关键字白名单或正则，例如：

- `_TAB_HINTS = {"首页", "我的", "造物秀", ...}`
- `_HOME_ORDER = ("首页", "发现", ...)`
- `KIND_LABELS = { feed_grid: '信息流', ... }` 把框架枚举写死成固定中文页名表
- `landmark: str = "首页"` / `else "首页"` 作为校准占位默认值
- `_SKIP_RE = re.compile(r"协议|隐私条款|...")`
- `_LOGIN_RE = re.compile(r"登录|手机号|...")`
- `if text in {"关注", "粉丝", "作品"}` 用于判断页面类型

这类代码无法兼容别的软件，且会在底栏全 Tab 共现时产生误判（如把「我的」内容标到「首页」）。

## 允许的信号

只用**结构/几何/频次**，不猜文案含义：

| 场景 | 允许 |
|------|------|
| Tab 栏发现 | 底栏垂直带、`content_bottom_px`、短文本形态、左右排序、共现频次 |
| 当前 Tab | `localized.chosen`、`selected`/`checked`、resource-id 含 `selected/active/checked` |
| 时间线分段 | 内容指纹 Jaccard、layout framework、semantic role |
| 登录屏 | 多个 `EditText` / `password` 字段 |
| 噪声过滤 | 长度、纯数字、时钟格式 `^\d{1,2}:\d{2}$` |
| Chrome 区域 | resource-id/class 正则（`tab_bar`、`bottom_nav` 等 Android 通用 id） |

## 发现违例时

1. `rg` 搜索 `_HINTS`、`_HOME_`、中文 Tab 名 frozenset、`_LOGIN_RE`、`_SKIP_RE`
2. 改为结构推断；必要时抽到 `nav_screen_layout` / `nav_synthesis.assign_turn_tabs`
3. 不在注释里保留「示例 App 文案」当逻辑依据

## 相关文档

- `CLAUDE.md` §1 约束 4：导航配置在 `nav_fsm*` 表，**代码里不得硬编码被测 App 的文案或包名**
- `docs/NAVIGATION_ATLAS.md`
