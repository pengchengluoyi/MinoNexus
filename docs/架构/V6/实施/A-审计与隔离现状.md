# A 阶段只读审计与隔离现状

执行日期：2026-10-15。本次为源码静态审计与本机只读检查，不是 A 阶段整体准出。未启动/停止服务、未导入业务应用、未连接业务数据库、未执行迁移或 bootstrap；未改运行配置、服务登记和业务代码。

## 1. 端口与启动观察

| 目标 | 本次观察 | 结论 |
| --- | --- | --- |
| Nexus 10104 | Python PID 5027，监听所有 IPv4 地址；cwd 为 MinoNexus 仓库，父 PID 1；GET /health 200 | 服务在线，但不是已确认的独立安装版。父 PID 1 也可能是后台进程被收养，不能证明 launchd 管理 |
| Workbench 5273 | node PID 11345，cwd 为 MinoWorkbench 仓库；页面含 /@vite/client | 正在运行 Vite 开发服务，5273 已占用，不可直接另起同端口正式前端 |
| Scout 安装登记 | 找到用户 LaunchAgent com.mino.scout，入口为安装目录 bin/mino-scout，RunAtLoad=true，KeepAlive.Crashed=true | 已有独立安装登记，未读取 token；本次未确认所有 Scout 进程/实际连接 |
| Nexus/Workbench 登记 | 用户/系统 LaunchAgents、LaunchDaemons 中按产品名称未找到对应 plist | 没有发现产品命名登记；非标准命名及其他启动管理方式未排除 |
| 测试 Nexus 11104 / Workbench 5274 | 本次监听快照未发现监听者 | 可作拟用端口，尚未实际 bind，非永久保留 |
| PyCharm | 2026.1，PID 41515；观察到回环 63342 等端口；GET 63342/mcp 返回 404 | 不能把 IDE 内置端口视为已启用 MCP；等待用户确认实际端点，未完成工具发现/多项目/停止能力实测 |
| ADB | 127.0.0.1:5037 有 adb 监听 | 已有共享 server，不另起或 kill；设备归属仍需明确 |

lsof 未发现 Nexus 当时打开的 DB 文件，可能因连接生命周期短；不能据此推断数据根。没有为确认路径而读取进程环境或数据库。端口快照仅反映检查时刻，启动仍须 strict bind。

## 2. 当前隔离是否已经完成

结论：**尚无证据证明正式和测试六个运行实例已按方案分离。** 目前确定 Nexus 的进程 cwd 在源码仓库，Workbench 是开发服务器；既有 Scout 有安装登记。正式基线仍待部署，不可把设计表当本机现状。

| 隔离层 | 方案要求 | 本次状态 |
| --- | --- | --- |
| 程序 | 正式固定 releases，测试源码/venv | Nexus/Workbench 独立正式安装未确认 |
| 数据 | 正式登记原库，测试 .mino-test/nexus/mino.db | 实际数据根未读取，独立配置待落实 |
| 节点身份 | 两套 Scout 独立 HOME/id/token，各连对应 Nexus | 未读取凭据，未验证交叉注册拒绝 |
| 网络 | 正式 10104，测试显式端口；前端各自目标 | 10104/5273 正在用，11104/5274 未见监听 |
| 登录/浏览器 | origin、token存储、profile分离 | 实施尚待完成，不能靠端口隔离替代 |
| 秘密/设备 | 测试不继承正式秘密，设备独占计划 | 待配置；同用户不构成恶意代码安全边界 |

.gitignore 已验证覆盖 .mino-test/、其中 token、DB/WAL/SHM；本次 git ls-files 查询未见 .mino-test、.db及其WAL/SHM被跟踪。未删除文件或改索引。

## 3. 路径与初始化审计

- paths.py 在模块级计算 APP_DATA_DIR 并 mkdir；database.py 在导入时构建 engine，实际连接及建表可能晚于创建 engine。
- app.py 先导入路由/服务，随后 create_app；不能只在 lifespan 设置数据根。
- CLI 的 catalog/session/nav 等分支调用 ensure_db；rPacks 和 dispatch_log 等也有调用。直接导入点与调用位置见静态索引；这是直接依赖清单，不宣称穷尽动态导入路径。
- ensure_db 每进程 _ready 短路，首次执行先迁移前备份、create_all、run_auto_migration、bootstrap；其中多个子函数独立提交，不是一笔总事务。
- 结论：A 阶段优先落实入口环境与路径校验；不为审计运行现有启动命令，避免触及真实库。

## 4. 迁移实际行为与可重入判断

| 步骤 | 静态观察 | 准出判断 |
| --- | --- | --- |
| 缺列 ADD COLUMN | 查询存在性后执行 | 有重复执行保护；并发执行与旧代码兼容未演练 |
| binding_key 回填 | 只更新空值，随机生成绑定键 | 重复不再修改已填行；非纯 schema 操作 |
| owner 回填 | settings marker 控制数据迁移，随后建索引 | marker 有保护；半程提交/旧版兼容需副本演练 |
| email 归一化 | 扫描数据，变化时更新，再建唯一索引 | 可能遇到历史冲突，不能凭 SQL 判为必然成功 |
| DROP app_cases | 表存在时删 | 重复删除可跳过，但不可逆；需证明目标旧版不依赖该表 |
| FTS/索引 | ensure_fts、CREATE INDEX IF NOT EXISTS | 需核对失败传播与数据一致性 |
| 清理 user_plugin_secrets、unlink 账号文件 | 函数定义存在，但当前 run_auto_migration 未调用，末尾注释明确保留历史文件 | 纠正之前“当前启动一定清理”的表述；未来重新接入须重新审计 |
| 后半段异常 | try/except 捕获后记录 migration skipped，仍记录 schema ready | 离线 migrate 不能只看退出成功/日志，需明确 schema 后置条件；否则可漏报失败 |

本次没有执行两遍迁移，也未对正式库副本演练，幂等性与自动回退兼容性均未判通过。

## 5. bootstrap 独立审计

bootstrap 不只是写几条默认数据：

- load_all 扫描 prompts、更新内存状态；配置错误可使初始化失败。
- migrate_legacy：知识条目按 ID 跳过已有项；会从 settings.payload 移除旧 knowledge 字段，读取 learned YAML。
- recovery/resource-transition/guard seed：按已有记录判断新增，但升级函数仍会调整已有能力。
- seed_global_flow_blocks：已有版本不等于内置版本时覆盖 steps/version；删除 retired block；独立 Session 内 commit。旧版重新启动可能反向覆盖内置版本，不能把“有版本字段”当单调升级保证。
- key_ref backfill、read_web_auth、skill_channel 等有各自写入路径；完整调用名见索引。
- bootstrap 外层 rollback 不能撤销子函数其他 Session 已提交的更改。

处置：备份在任何初始化之前；副本验证两次重复启动、失败重试和旧版重启的语义，而非只检查新增行数量。本次只完成静态风险识别。

## 6. 写者与退出边界首轮清单

| 写者/入口 | 写入或副作用 | 停服/更新需要核对 |
| --- | --- | --- |
| lifespan 初始化 | ensure_db、seed users、僵尸run清扫、租约释放/过期清理 | 启动不是只读；旧版恢复也会写 |
| session_store / dispatch_log | 事件、调用日志及文件 | 活动run排空，确保尾部写入完成 |
| node_registry/心跳 | 节点快照和注册相关状态 | 空闲仍可能有心跳，停旧服务后备份 |
| 文档同步循环 | 每60秒tick、外部飞书读取/同步及库写入 | cancel 后退出语义需验证 |
| 助手通知重试 | 每30秒循环，to_thread 外发及状态更新 | asyncio cancel 不保证已进入线程的发送停止 |
| nav采集/编译、用例导入 | 请求驱动和线程池写入 | 统计活动操作，不只盯 run 列表 |
| LLM执行/dispatch | LLM调用、结果和日志 | 真正执行链继续盘点；不能把 services/job_store 当持久作业队列 |
| wechat_ilink 轮询线程 | 条件启动线程、外发/收取与状态 | 是否启用及退出机制未运行验证 |

app lifespan 退出当前对两个异步循环调用 cancel，未在已读片段看到 await 等待；线程中在途副作用需单独处理。之前笔记的 ai/job_store.py 在当前树不存在，services/job_store.py 是提示词任务定义读取封装，不是 LLM 运行任务存储。定时派发的完整入口尚未完成追踪，不虚构已有统一 scheduler 停止接口。

## 7. 下一步与状态

已完成：只读端口/目录/产品服务登记快照、公开健康与前端类型检查、直接导入索引、迁移/ bootstrap 静态审计、忽略规则检查。

待用户提供：PyCharm MCP 已启用端点（不提供密钥），再做 initialize/tools-list/项目选择只读实测。

待实施/演练：正式安装布局与数据根登记；迁移/旧版恢复副本演练；全部后台写者退出核对；probe token、能力注册、CI manifest、安装器原型。本轮未启动这些修改，A 阶段不标完成。
