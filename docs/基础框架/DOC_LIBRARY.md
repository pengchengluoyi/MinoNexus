# 文档库（PDF / Markdown 全文检索）

**产品真源。** 上传需求文档、PRD、接口说明等原始文件，分片入库，按关键词全文检索。

与 **执行知识条**（`knowledge_entries`，见 [HTTP.md](HTTP.md) §知识库）是两条线：

| | 文档库 | 执行知识条 |
|---|---|---|
| 内容形态 | 原始 PDF / `.md` 全文 | 短条「可验证口径」（标题 + 正文） |
| 写入方式 | **上传文件**（离线 ingest） | 手填 / 跑批沉淀 / 失败分析 |
| 检索 | **FTS5 全文检索**（执行期可预加载索引） | `knowledge_match` 情境匹配 |
| 注入 prompt | 二期：检索 top-K 片段；一期仅 API 搜索 | 已在 `agent_loop` / `nav_compiler` |
| 与 NavFSM | 无直接绑定 | `wiki_ref: knowledge:<id>` |

NavFSM 守卫上的 `wiki_ref` **只挂执行知识条**，不挂文档分片。文档库内容若要进跑批，须经 **DOC_LEARN 抽取** 落成 `knowledge_entries` 后再挂。

与导航、执行知识的统一编排（`context-pack`、`wiki_first`）见 **[9月12日_AppIntel信息基座.md](9月12日_AppIntel信息基座.md)**。

---

## 1. 存储

| 类型 | 归宿 |
|------|------|
| 元数据 | `mino.db` → `doc_sources` / `doc_chunks` |
| 全文索引 | `mino.db` → `doc_chunks_fts`（SQLite FTS5 虚拟表） |
| 原始文件 | `{data_dir}/docs/{app_id}/{source_id}/original.{md\|pdf}` |
| 分片正文 | 落在 `doc_chunks.text`；超长正文可选落 `{data_dir}/docs/.../chunks/`（一期全在 DB） |

键与现有模型一致：`app_id`（必填）、`project_id`（可选，来自 `apps.project_id`）。

---

## 2. Ingest 流水线（离线）

```mermaid
flowchart LR
  U["上传 .md / .pdf"] --> P["解析正文"]
  P --> C["分片 ≤2000 字/片<br/>md 优先按标题层级"]
  C --> I["写入 doc_chunks + FTS5"]
  I --> S["doc_sources.status = ok"]
```

**约束（与上游 L2 方案一致）**：

- 执行期 **不** 现场读 PDF、不现场建索引；
- 上传与解析在 HTTP 请求内完成（小文件）；大文件可二期改后台任务；
- 同一 `app_id` + `content_hash` 重复上传 → **覆盖** 旧 `source_id` 对应分片与索引。

**依赖**：`pypdf` 解析 PDF；Markdown 按 UTF-8 直读。

---

## 3. HTTP API

Console「知识库」页与 Studio 后续共用 `/settings/docs*`（需登录）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/settings/docs` | 列表。`app_id` 必填；可选 `project_id` |
| POST | `/settings/docs/upload` | `multipart`：`file` + `app_id` + 可选 `project_id` / `title` |
| GET | `/settings/docs/{source_id}` | 单条元数据 |
| GET | `/settings/docs/{source_id}/chunks` | 分片列表（`offset` / `limit`） |
| GET | `/settings/docs/search` | 全文检索。`q` + `app_id` + 可选 `limit` |
| DELETE | `/settings/docs/{source_id}` | 删元数据、分片、FTS 行、原始文件 |

响应形状见 `mino_nexus/services/doc_store.py` 的 `_to_public`。

---

## 4. 与 NavFSM §6 的关系

[NAVIGATION_ATLAS.md](NAVIGATION_ATLAS.md) §6 描述的是 **执行知识条 + `wiki_ref` 注入**，不是文档库。  
文档库本文；二者通过未来的 **DOC_LEARN** 衔接，不在一期实现。

---

## 5. 跑批注入（Serve）

每步 `agent_loop` 在 `match_step_knowledge` 之后调用 `match_step_docs`：

- 用同一套 `build_query`（用例意图 + 步骤焦点 + 屏文案）对文档库做 FTS；
- 命中结果格式化为 `doc_context` 槽（≤1200 字），写入 `agent-decide` prompt（`prompt_version >= 9`）；
- 校验步 `assert_visual` 的 `knowledge_context` 也会附带文档摘录；
- session log 事件：`doc/match`。

检索失败或库为空 → **降级为空串**，不阻断跑批。

## 6. DOC_LEARN（文档 → 执行知识条）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/settings/docs/{source_id}/extract` | 按分片批调 LLM，写入 `knowledge_entries`（`source=doc`，`review_status=pending`） |

Console / Studio 文档库页 **「抽取知识」** 按钮。需配置大模型 Key（`case_execution_use`）。

## 7. 飞书同步

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/settings/docs/sync-feishu` | body：`url` + `app_id` + 可选 `bot_id` / `project_id` / `title` |

支持 `/wiki/` 节点（obj_type=docx）与 `/docx/` 直链。复用 Console「机器人」里的飞书 `app_id` / `app_secret`。  
同一 `app_id` + `source_url` 再次同步会**覆盖**旧索引。

## 8. 遇阻二次检索（T3）

当 `ProgressGate.fuse_block_streak > 0` 或 `no_progress_streak >= 2` 时，每用例最多 **2 次**额外 FTS（屏 hierarchy 文案加权），追加 `【遇阻文档提示】` 到 `doc_context`。

## 9. 混合检索（FTS + 向量）

| 阶段 | 行为 |
|------|------|
| ingest | 分片写入后离线调 OpenAI-compatible `/embeddings`，向量存 `doc_chunks.embedding_json` |
| 跑批 | 用例开始时 **1 次** `bootstrap_case_doc_query` 算 query 向量，逐步检索只做 FTS + 预存向量点积 |
| Console 检索 | `GET /settings/docs/search?vector=1` 时对 query 现场 embed（仅 UI） |

`hybrid_score = overlap×10 − bm25×0.001 + vector_score×5`。无 Key 或 embed 失败 → 退化为 FTS + 字面混合。

**火山引擎 / 豆包**：embedding 模型与聊天模型不同，需在方舟控制台开通文本向量模型，或将 **接入点 ID**（`ep-…`）写入 provider 的 `embedding_model`（或环境变量 `MINO_EMBEDDING_MODEL`）。账号无权限时 Nexus 会停用该 provider 的 embedding 并只打一条警告，不再刷 404 日志。

## 10. 飞书定时增量同步

| 字段 | 说明 |
|------|------|
| `auto_sync` | 1=启用（飞书导入默认开） |
| `sync_interval_sec` | 最短 300s，默认 3600 |

Nexus 启动后每 60s `doc_sync_tick`：拉取 `source_kind=feishu` 且到期的文档，比 `content_hash` 无变化则只 `touch`，有变化则重 ingest。

| 方法 | 路径 | 说明 |
|------|------|------|
| PATCH | `/settings/docs/{id}/sync` | body：`auto_sync` / `sync_interval_sec` |
| POST | `/settings/docs/{id}/sync-now` | 立即重拉飞书正文 |
