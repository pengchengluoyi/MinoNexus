# AppIntel 信息基座 — 设计方案

**日期**：2026-09-12  
**版本**：v0.2（已实现 P0–P4 后端）  
**状态**：后端 + Console「信息基座」页 + Studio 导航图徽标已落地。

**关联文档**：

| 文档 | 关系 |
|------|------|
| [NAVIGATION_ATLAS.md](NAVIGATION_ATLAS.md) | 渠道 A：操作导航图 NavFSM |
| [DOC_LIBRARY.md](DOC_LIBRARY.md) | 渠道 C：原始文档库 |
| [HTTP.md](HTTP.md) §知识库 | 渠道 B：执行知识条 `knowledge_entries` |
| [PROMPTS.md](PROMPTS.md) | `agent-decide` 槽：`nav_assist` / `knowledge_*` / `doc_context` |
| [DATA_MODEL.md](DATA_MODEL.md) | 三渠道 ORM 真源表 |

---

## 1. 背景与动机

同一被测应用（`app_id` + `project_id`）上，Nexus 已有三套并行能力：

| 渠道 | 记什么 | 真源 | 跑批消费 |
|------|--------|------|----------|
| **导航 NavFSM** | 在哪、去哪、允许点什么 | `nav_fsm*` | `nav_assist`（`NavRuntime` → `nav_compiler`） |
| **执行知识 Wiki** | 为什么、怎么判、例外 | `knowledge_entries` + guard `wiki_ref` | `knowledge_hint` / `knowledge_body` |
| **文档库 Doc** | PRD / 接口原文 | `doc_sources` / `doc_chunks` + FTS | `doc_context`（每步 top-K 片段） |

问题在于 **写入形态不同、读取各走各的 API、缺少交叉引用与统一编排**：

- Console「全文检索」只搜文档；跑批每步独立 `match_step_docs`，与知识、导航无协同。
- NavFSM 上 `wiki_ref` 已设计为挂知识条，但缺「无 wiki_ref 的 state 告警」「从 PRD 反链补知识」。
- DOC_LEARN 可把文档落成知识条，但未与导航候选、失败沉淀进同一待审队列。

市面 **LLM Wiki**（Karpathy 模式）强调：ingest 时 **编译** 持久 wiki，query 时读已编译页，而非每次重读 PDF。本仓 **不部署独立 llm-wiki 服务**；编译层由 **`knowledge_entries` + `wiki_ref`** 承担，原始真源由 **文档库** 承担，操作结构由 **NavFSM** 承担。三者应作为 **同一 App 的信息基座上的三个渠道**，而非三个孤岛。

---

## 2. 目标与非目标

### 2.1 目标

1. **统一作用域**：一切读写以 `app_id`（及 `project_id`）为键，与 `RunContext`、`nav_fsm_store`、`knowledge_store`、`doc_store` 一致。
2. **三向互通**：文档 → 知识 → 导航、导航 → 知识 → 文档、跑批/session → 三者沉淀，均有明确管道与人工审核点。
3. **统一查询面**：对外提供 `AppIntel` 服务（规划路径 `/apps/{app_id}/intel/*`），支撑跑批 `context-pack`、Console 问答、路线导航、关联图谱。
4. **agent_loop 单入口出参**：用一次 `context_pack()` 替代分散的 `match_step_knowledge` + `match_step_docs` + 零散 nav 文本拼装；策略可控（如 `wiki_first`）。
5. **引用可追溯**：每步决策附带 `citations[]`（nav / knowledge / doc 混排），便于 Studio 调试与审核。

### 2.2 非目标（第一期不做）

- 不引入第三套「Obsidian 式」几百页 Markdown wiki 真源（编译结果仍是 `knowledge_entries`）。
- 不把整篇 PRD 塞进 prompt（文档渠道仅 top-K 片段，预算见 §6）。
- 不自动把 runtime 候选合并进 `nav_fsm*`（仍须人审，见 [NAVIGATION_ATLAS.md](NAVIGATION_ATLAS.md) §7）。
- 不在 Nexus 内做 OCR / 扫描件 PDF 识别（见 [CLAUDE.md](../CLAUDE.md) §1）。

---

## 3. 概念：两个「图」+ 一个「库」

勿与 NavFSM 混淆：

| 名称 | 是什么 | 本仓对应 |
|------|--------|----------|
| **操作导航图** | 屏态、边、守卫、路径规划 | **NavFSM**（已实现） |
| **知识关联** | 短条、分面、挂 guard、溯源 PRD | **knowledge_entries** + `wiki_ref` |
| **原始文档库** | 不可变真源、全文检索 | **doc_***（已实现） |

「图谱导航」在 Studio 上的目标形态：**以 NavFSM 为骨架**，节点上展示挂接的知识条与文档溯源（§8 P2），不是另建一套与 FSM 无关的知识图谱产品。

---

## 4. 架构总览

```
                    ┌─────────────────────────────────────┐
                    │  消费方                              │
                    │  agent_loop · Console · Studio · MCP │
                    └──────────────────┬──────────────────┘
                                       │
                    ┌──────────────────▼──────────────────┐
                    │  AppIntel 信息基座（规划）             │
                    │  · 统一索引（FTS + 可选向量）          │
                    │  · 关联解析（ref / wiki_ref / links）  │
                    │  · 策略（wiki_first · token 预算）     │
                    │  · proposals 待审队列                  │
                    └──────────┬────────────┬───────────────┘
                               │            │
         ┌─────────────────────┼────────────┼─────────────────────┐
         ▼                     ▼            ▼                     ▼
   nav_fsm*            knowledge_entries   doc_sources/chunks   session_events
   （渠道 A）            （渠道 B）           （渠道 C）            （回流）
```

**铁律**：三渠道的 **ORM 真源表不变**；AppIntel 是 **投影 + 编排层**（`services/app_intel.py`，规划），不复制正文到第四张「大宽表」。

---

## 5. 逻辑条目模型（AppIntelEntry）

用于统一检索与图谱 API 的 **逻辑视图**（可先由代码 join 生成，二期可加 `app_intel_links` 表）。

```json
{
  "app_id": "3d2b9799-...",
  "project_id": "...",
  "kind": "nav_state | nav_edge | knowledge | doc_chunk",
  "ref": "nav:page.community | knowledge:abc12 | doc:chunk:9e80af",
  "title": "社区页",
  "summary": "双列 feed，爱心可直点…",
  "tags": ["社区", "p0"],
  "situation": {
    "screen_role": "feed_list",
    "need": "howto",
    "facet": "..."
  },
  "links": [
    { "rel": "wiki_ref", "target": "knowledge:abc12" },
    { "rel": "sourced_from", "target": "doc:chunk:9e80af" }
  ],
  "provenance": {
    "source_kind": "doc_learn | manual | run_sediment | nav_calibrate",
    "source_id": "...",
    "updated_at": 1789200000
  }
}
```

### 5.1 关联类型（`rel`）

| rel | 含义 | 示例 |
|-----|------|------|
| `wiki_ref` | Nav guard → 知识条 | FSM `guards.hidden.wiki_ref` |
| `sourced_from` | 知识条 → 文档分片 | `knowledge_entries.source=doc` |
| `describes_state` | 知识 → 屏态 | `knowledge_situation.screen_role` |
| `landmark_in_doc` | 屏态文案 ↔ PRD | 人工或 lint 建议 |
| `supersedes` | 知识版本替代 | 审核换条时 |

---

## 6. 三向数据填充（闭环）

### 6.1 文档 → 知识 → 导航

```
上传/飞书 → doc_ingest → doc_chunks + FTS
         → DOC_LEARN（POST /settings/docs/{id}/extract）
         → knowledge_entries（review_status=pending）
         → Console 审核 approve
         → 挂 nav_fsm_states.guards.*.wiki_ref = knowledge:<id>
```

### 6.2 导航 → 知识 → 文档

```
探索/校准/walkthrough → nav 候选（边/守卫/detect）
                    → publish 进 nav_fsm*
                    → gaps API：无 wiki_ref 的 state 列表
                    → 可选：AppIntel.search 从 doc 推荐段落 → DOC_LEARN
```

### 6.3 跑批 / Session → 三者

| 事件 | 写入渠道 | 入口（现有/规划） |
|------|----------|-------------------|
| 步骤失败 | 知识候选 | `POST /settings/knowledge/analyze-failure` |
| 低置信 localize | Nav 候选 + doc 片段 | nav 遥测 + `match_stuck_docs` |
| 成功路径 | 边权重 / 可选知识 | `nav_telemetry`、跑批沉淀 |
| 模型点名知识 | 展开正文 | `expand_named_knowledge`（已有） |

**待审统一队列（规划 P4）**：`app_intel_proposals` 表或 `settings.payload` 子树，类型 `nav_patch | knowledge_draft | doc_learn_batch`，Console 一页审完。

---

## 7. 统一 HTTP API（规划）

前缀：**`/apps/{app_id}/intel`**（需登录；`project_id` 可选校验与 `apps` 一致）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/search` | `q` + `kinds=nav,knowledge,doc` + 可选 `state_id` / `screen` |
| POST | `/context-pack` | **跑批主入口**；见 §7.1 |
| GET | `/route` | `from_state` / `to_state` → 路径 + 边摘要 + 沿途 wiki_ref |
| POST | `/ask` | 问答（检索基座 + LLM，**必须带 citations**） |
| GET | `/graph` | `center=nav:page.x` → 邻接子图（nav + 挂接知识 + doc 溯源） |
| GET | `/gaps` | 缺 wiki_ref 的 state、无知识的边、空文档库等 |
| POST | `/proposals` | 提交待审（封装 DOC_LEARN / nav draft / 失败分析） |

实现模块（规划）：`mino_nexus/services/app_intel.py`  
路由（规划）：`mino_nexus/routers/rAppIntel.py` 或挂在 `rNavFsm` / 新 router。

### 7.1 `POST /context-pack`（P0 核心）

**请求**（示例）：

```json
{
  "intent": "用例意图 + 步骤焦点 + 屏文案…",
  "state_id": "page.community",
  "hierarchy_excerpt": "…",
  "policy": "wiki_first",
  "budgets": {
    "nav_assist": 1200,
    "knowledge": 1200,
    "doc_context": 800
  }
}
```

**响应**（示例）：

```json
{
  "nav_assist": "…",
  "knowledge_hint": "…",
  "knowledge_body": "…",
  "doc_context": "",
  "citations": [
    { "kind": "knowledge", "id": "abc12", "title": "社区点赞" },
    { "kind": "nav", "ref": "nav:page.community", "wiki_ref": "knowledge:abc12" }
  ],
  "policy_applied": "wiki_first",
  "skipped": ["doc_context"]
}
```

**策略 `wiki_first`（默认推荐）**：

1. 若有 `state_id`，解析 guard 上 `wiki_ref` → `nav_compiler.wiki_block`（已有）。
2. `match_step_knowledge`（已有）填满 `knowledge_hint` / 按需 `knowledge_body`。
3. 仅当知识侧命中不足（可配置阈值）时调用 `match_step_docs`。
4. 总 token 不超过 `budgets` 之和；冲突时 **知识 > 导航说明 > 文档摘录**。

### 7.2 与现有 API 的关系

| 现有 | 迁移 |
|------|------|
| `GET /settings/docs/search` | 保留；内部可调 `AppIntel.search(kinds=doc)` |
| `GET /settings/knowledge*` | 保留；写入真源不变 |
| `GET/PUT /nav-fsm/{app_id}` | 保留；图谱读侧可走 `/intel/graph` |

---

## 8. agent_loop 集成（规划）

### 8.1 现状（分散）

```text
inspect_slots["knowledge_hint"] = match_step_knowledge(...)
inspect_slots["doc_context"]    = match_step_docs(...)
NavRuntime → slot_sink["nav_assist"] = nav_compiler.compile_assist(...)
```

### 8.2 目标（单入口）

```text
pack = app_intel.context_pack(ctx, case, cursor, hierarchy_text, nav=nav_runtime, policy="wiki_first")
inspect_slots.update(pack.to_slots())  # nav_assist, knowledge_*, doc_context
session_log.emit("intel/context_pack", citations=pack.citations)
```

### 8.3 预期提升

| 方向 | 机制 |
|------|------|
| 少空转 | 屏态 + howto 知识 + 允许动作集一致 |
| 少幻觉 | 校验用已审 **judge** 条，非 PRD 碎片猜 |
| 省 token | `wiki_first` 下多数步 `doc_context` 为空 |
| 遇阻 | T3 优先 exception 知识 + wiki_ref，doc 兜底 |
| 可调试 | `citations` 与 session log 对齐 |

**约束**：`agent_loop` 仍 **不感知 Scout 网络**；`context_pack` 仅读库与内存中的 `NavRuntime`，不发设备指令。

---

## 9. 与市面 LLM Wiki 的对照

| Karpathy LLM Wiki | MinoNexus AppIntel |
|-------------------|---------------------|
| `sources/` 只读真源 | **文档库** + 飞书 |
| `wiki/*.md` LLM 维护 | **`knowledge_entries`**（人审 + DOC_LEARN） |
| `index.md` 导航 | **`knowledge_hint` + `/intel/search`**（规划） |
| `schema` / AGENTS.md | **PROMPTS + situation facet + NavFSM schema** |
| Query 读 wiki 不读 raw | **`wiki_ref` + knowledge_match**；doc 兜底 |
| Graph 浏览 | **NavFSM 图 + `/intel/graph`**（规划） |

混合策略与业界一致：**稳定、已编译知识走 Wiki 渠道；volatile 或大块原文走 Doc 渠道；操作走 Nav 渠道**。

---

## 10. 分阶段落地

| 阶段 | 交付 | 改动面 |
|------|------|--------|
| **P0** | ✅ `context_pack_for_step` + `wiki_first`；`agent_loop`；`intel/context_pack` | 已落地 |
| **P1** | ✅ `/intel/search`、`/intel/gaps`、`app_intel_links` 表 | Console「信息基座」页 |
| **P2** | ✅ `/intel/graph` | Studio 导航节点徽标（知/文/缺 wiki） |
| **P3** | ✅ `/intel/ask` | Console 问答 Tab |
| **P4** | ✅ `app_intel_proposals` + `/intel/proposals` | Console 待审队列 |

**P0 不强制新表**：`wiki_ref` 解析、`items_by_ids`、`match_step_knowledge`、`match_step_docs`、`nav_compiler.compile_assist` 包一层即可。

---

## 11. 数据模型补充（规划）

在 [DATA_MODEL.md](DATA_MODEL.md) 真源表之外，可选：

```sql
-- P1 起，可选
CREATE TABLE app_intel_links (
  id            INTEGER PRIMARY KEY,
  app_id        TEXT NOT NULL,
  from_ref      TEXT NOT NULL,   -- nav:... | knowledge:... | doc:chunk:...
  to_ref        TEXT NOT NULL,
  rel           TEXT NOT NULL,
  meta          JSON,
  created_at    INTEGER
);
CREATE INDEX idx_intel_links_app ON app_intel_links(app_id);
```

`wiki_ref` 仍写在 `nav_fsm_states.guards` JSON 内为 **权威挂接**；`app_intel_links` 用于 **额外** 溯源与图谱边，避免重复维护时以 `wiki_ref` 为准。

---

## 12. 改动前检查清单

动 AppIntel / agent_loop 注入前确认：

1. 读 [NAVIGATION_ATLAS.md](NAVIGATION_ATLAS.md) §6 — 文档库 ≠ 执行知识条。
2. 读 [DOC_LIBRARY.md](DOC_LIBRARY.md) — 执行期不现场解析 PDF；`doc_context` 有字数上限。
3. 读 [PROMPTS.md](PROMPTS.md) — `agent-decide` 槽版本（`doc_context` ≥ v9）。
4. Nav 代码不硬编码被测 App 文案（[no-app-keywords](../.cursor/skills/no-app-keywords/SKILL.md)）。
5. 循环代码不出现 `scout_connected` 等网络分支（[CLAUDE.md](../CLAUDE.md) §2.2）。

---

## 13. 修订记录

| 日期 | 说明 |
|------|------|
| 2026-09-12 | 初稿：三渠道统一基座、context-pack、分阶段路线 |
| 2026-09-12 | v0.2：后端 P0–P4 落地，`agent_loop` 接入 `wiki_first` |
