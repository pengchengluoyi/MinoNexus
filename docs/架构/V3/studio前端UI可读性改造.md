# Studio 前端 UI 可读性改造（V3）

> 范围：**MinoStudio** 测试工作台（执行批次、用例步骤、新建执行）。目标与 UI 稿一致：**加大留白**、信息层次 **标题 → 关键结论 → 明细**，并靠栅格与固定侧栏解决宽屏/窄屏自适应，而不是堆叠大量边框和色块。

## 设计原则

| 原则 | 落地方式 |
|------|----------|
| 三层信息 | 页眉只保留标题 + 状态；中间放「结论卡 / 数字卡 / 结果卡」；底部 Tab + 表格/时间线为明细 |
| 留白与分隔 | 弱化 `border` / 灰底块，用 `gap`、`border-bottom`、浅分割线区分区块 |
| 状态语义统一 | `--mo-status-pass/fail/warn/cancel` + `.mo-status-pill` 全站复用 |
| 自适应 | 批次统计 5 列 → `<1100px` 时进度卡通栏、其余 2 列；用例步骤 **主栏 + 380px 右栏** → `<1100px` 上下堆叠 |

## 全局

**文件**

- `src/styles/studio-readability.css`：可读性组件与 CSS 变量（在 `main.js` 全局引入）
- `src/layouts/work-shell.css`：侧栏选中态保留浅色底，去掉彩色圆点占位
- `src/views/Testing/AppShell.vue`：测试导航改为 Element Plus **单色线性图标**（`nav-icon-wrap`）

**侧栏**

- 原 `nav-dot` 彩色圆点 + Emoji 改为 `CollectionTag` / `List` / `Reading` 等图标
- 选中项仍为 `var(--mo-primary-soft)` 浅底 + 主色字

## 批次详情（TaskDetailPane）

**文件**：`src/views/Testing/TaskDetailPane.vue`

### 页眉

- 标题放大（`.pane-title-lg`），状态用 `.mo-status-pill`
- 去掉页眉内通过的/失败的零散数字与 meta chips
- 操作区降级：`text` 按钮；**主按钮仅保留插槽里的「新建执行」**（由 `AppShell` 传入）

### 信息卡 + 五张数字卡

- **信息卡** `.batch-info-card`：应用、环境、端、设备、模型、耗时；测试包单独一行 `span-full`
- **数字卡** `.batch-stat-grid`：执行进度（条 + %）、通过、失败、不可做、还没测到（文案与测试报告图例一致）

### Tab

- 原两行高 `settings-tabbar` 改为 `.mo-tab-strip`：用例（带总数）、测试报告、签收、任务详情、待审核知识（带数量）

### 用例 Tab

- `.mo-filter-pills`：全部 / 失败 / 通过 / 已取消（带计数）
- 表格去竖线边框，行可点进步骤；**失败摘要贴在用例名下方**（`.case-table-fail`），右侧 `›` 暗示可点

### 测试报告 Tab

- 顶部 `.report-legend` 说明四种状态含义
- 数量为 0 的分组改为 `.report-table.is-zero` 单行折叠，有数据才展开完整 `el-table`

## 新建执行弹窗（AppShell）

**文件**：`src/views/Testing/AppShell.vue`

- 表单项分为编号步骤 **1～5**（被测应用 → 环境+设备并排 → 覆盖方式 → 执行方案 → 勾选用例）
- 卡片选中显示 **✓**（`.run-pick-card` + `.pick-check`），长说明在 `.pick-desc` 下方
- Footer `.new-run-footer-bar`：左侧「已选 N 条用例」，右侧启动（仍受 `canStartRun` 约束，未勾选用例不可点）

## 用例步骤页（ExecutionTimeline + StepDetail）

**文件**

- `src/components/ExecutionTimeline.vue`：`layout="case"`（由 `TaskDetailPane` 传入）
- `src/components/ExecutionStepDetailDrawer.vue`：`mode="panel"` 嵌入右侧栏

### 布局

- `.et-wrap.is-case-layout`：`grid` 左主右栏；主栏 `.et-case-main`，右栏 `.et-case-rail` 固定步骤详情
- 窄屏右栏改为 `max-height: 42vh` 叠在下方

### 关键结论

- 执行结果卡默认展开（`.et-verdict-hero`），大图截图 + 一句话原因
- 有巡检/摘要时展示 **预期 / 实际** 双栏（`.et-compare-row`）

### 前置 / 操作 / 校验

- 三列 `.et-phase-board`：完整任务标题、步骤区间（`#n` 或 `#n~#m`），点击跳转对应步
- 有三列结构时隐藏下方冗长步骤树，避免重复

### 时间线

- 色条宽度仍按耗时比例（`waterfallBars`）；胶片横滑与右栏 `activeStep` 同步
- 时间线区在 case 布局下 `.et-fold.is-flat` 去掉折叠头，减少一层点击

### 右侧详情栏

- Tab：**这一步** / **全部步骤**；角标 `当前序号 / 总步数`
- 长判定默认折叠（`.sd-clamp` + 展开）；**原始事件**（LLM in/out）默认收起

### 数据与后续接口

- 步骤、批次、报告数据仍来自现有 Nexus / `caseRunner` 拉取逻辑；未改 API 契约
- 若原型里 `lib/data.ts` 仅用于静态预览，Studio 侧以真实 `task.cases`、`getCaseRunnerTraceDetail` 等为准，字段不齐时 compare 区可能为空（可后续从 `inspections` 结构化补齐）

## 自测建议

1. **执行批次列表** → 进入批次 → 看信息卡、五数字卡、用例筛选与行内失败摘要
2. **点失败用例** → 左：结果卡 + 三列 + 时间线；右：步骤 Tab 与全部步骤列表
3. **新建执行** → 无勾选用例时启动禁用；勾选后 footer 计数更新
4. 缩浏览器宽度至 `<1100px`，确认数字卡与用例步骤上下布局无横向溢出

## 测试入口简化（去掉项目中间层）

**文件**：`src/views/Testing/AppList.vue`

- 移除「项目 / 实验室排期 / 管理」Tab 及应用列表中间页（原红框区域）
- 侧栏点击项目 **直接进入工作台**（默认 Tab：`tasks` / 执行批次）
- `/testing` 加载后自动 `openProject`，主区仅保留轻量「正在进入工作台…」占位

## 执行批次列表

**文件**：`src/views/Testing/AppShell.vue` + `studio-readability.css`

- 去掉 `el-table` 边框表，改为 **行列表**（状态 pill + 标题 + 元信息 + 细进度条 + 整行可点）

## 相关改动文件清单

```
src/main.js
src/styles/studio-readability.css
src/layouts/work-shell.css
src/views/Testing/AppShell.vue
src/views/Testing/TaskDetailPane.vue
src/components/ExecutionTimeline.vue
src/components/ExecutionStepDetailDrawer.vue
```
