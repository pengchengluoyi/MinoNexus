# A：审计与基础交付

以主方案 V12 为准。以下是当前代码线索，不是已完成实现；路径相对对应仓库，行号不固定。

## 数据根、初始化与后台写者

- Nexus `core/paths.py` 的模块级 APP_DATA_DIR 会创建目录；`core/database.py` 导入时创建 engine。审计 CLI/ASGI/维护命令和间接导入，确保测试配置先于副作用。
- ensure_db 的 _ready 只是进程内短路；新进程仍会执行 create_all、run_auto_migration、bootstrap。rebind_sqlite_to_data_dir 不是正式启动接口。
- 当前 `core/migration.py` 不止 additive ALTER：有 DROP TABLE、数据 UPDATE、秘密清理及文件处理路径。审计实际调用与触发条件，不能凭模块说明归类为纯 additive。
- 清单至少涵盖 session 事件、心跳/节点快照、LLM/job_store、导航采集、调度和 app lifespan 的初始化/修复/同步任务。输出每项入口、数据/文件副作用、停止/恢复方法，标明哪些任务属于活动任务统计。
- **bootstrap 单列审计行**：记录默认行写入、覆盖/去重规则、文件或外部副作用、失败后重跑和新旧版本重复执行的结果。离线 CLI 若复用 ensure_db，会同时执行 bootstrap，备份必须发生在 create_all/迁移/bootstrap 之前；不能只验证 ALTER 可重入就认定整个初始化安全。
- `.gitignore` 已补 `.mino-test/`、目录内 token 以及 SQLite DB/WAL/SHM/journal 规则；A 验收核对忽略规则和已跟踪清单。规则不会自动取消已跟踪文件，不自动删除或移除索引。
- 不全量重构 ensure_db 模式。核实重复执行和旧版启动兼容性；仅修阻碍首期版本组合的实际问题。

A 阶段必须修复并验证的两个问题（2026-10-17 已完成本轮源码修复及隔离验证，正式进程未重启）：

- run_auto_migration 后半段捕获异常后仍记录 schema ready：让失败向调用者传播，离线 migrate 必须非零退出，ensure_db 不得在失败后标记 ready。用隔离副本实际触发一次迁移失败，检查退出状态、日志与 ready 状态；schema 后置检查不能替代异常传播。
- seed_global_flow_blocks 的版本不等覆盖：改为明确版本序列的只升不降，同版保留，旧版启动不得覆盖新版 steps/version；未知版本不可猜顺序。同时检查 retired 删除是否影响新版数据。用新版种子写入的隔离副本运行旧版初始化验证；已发布旧版无法满足时，该组合禁止自动回退，转人工维护。

## PyCharm 与 MCP

本机连接、工具发现及 Workbench dev 启动/输出/端口冲突实测已完成，见 [PyCharm 实测结果](A-PyCharm实测结果.md)。三仓共用 MinoNexus 项目入口；长驻执行返回 timedOut=true 后服务继续运行。尚未通过产品 Scout 链，独立停止/重启接口缺失；Nexus/Scout 测试实例未启动，环境隔离另行验收。

实际验证手动启用、传输、认证、工具、停止/状态查询、多项目路由。窗口与进程端口不预设对应，应用级配置可覆盖连接。项目选择与焦点变化不能改变执行目标；样例项目排他使用。

AssistantMcpToken 当前无权限 scope；rMcp 的 token_scope 是写操作去重上下文。resolve_mcp_token 只返回用户，认证会更新最近使用时间；initialize/ping/tools/list 不经过 _call。probe 的权限检查应在方法分发前，普通 API token 解析也必须拒绝 probe。

## 安装与 CI 并行

登记本机 Nexus、Workbench、PyCharm MCP 及 Scout 辅助监听的地址/端口，检查现有占用并记录使用方。5273/5274 是本方案自定端口；当前 Workbench vite.config.ts 已使用 5273，不能因 Vite 通用默认值为 5173 就判定 5273 空闲。预检查后启动仍必须 strict bind，冲突失败且不得自动停其他进程。尚未实测的端口标为未核实。

当前审计确认 Nexus 在源码目录运行、父 PID 为 1；没有确认可供更新器控制的 Nexus 服务登记，不能仅凭 PPID 1 推断由 launchd 托管。D2 前必须完成 Nexus/Workbench 正式基线安装及服务登记，并演练停启；不能直接把当前源码进程当成可更新安装版。

三仓各用自己的 GitHub Releases，共用 manifest_schema_version。首期定稿使用 manifest_schema_version=1，兼容保留 Scout version/items/layers，增加 component、build_id、source_commit、compatibility。item 明确 os/arch、URL、sha256、bytes；静态资源可声明平台无关，附带运行时按平台单列。

Scout scripts/write_manifest.py 当前只输出 version/items/layers，source_commit/build_id 需要贯通 CI、manifest 与运行版本查询。使用源码仓库真实提交，不拿打包仓库 SHA 代替。Nexus/Workbench 新建平台包与固定 tag 发布工作流，失败包不出现在可安装版本列表。

验收输出：导入/写者/迁移审计表（bootstrap 独立一行）、忽略规则与跟踪清单检查、监听地址/端口登记及占用结果、IDE 实测表、安装基线原型、manifest schema 1 与三仓样例实际包。基础设施不介入 Git 提交审批。


## 2026-10-17 实施进展

见 [A 阶段实施记录](A-实施记录-20261017.md)。迁移异常传播、种子版本保护、过时 bootstrap 调用及 Nexus 显式隔离启动已修复并实测；旧 HEAD seed 已证实不兼容新版数据，禁止据此自动回退。完整写者审计、安装服务基线、probe、manifest/CI 与三仓实际包仍待完成，A 阶段保持进行中。


## 2026-10-17 后续交付

完整审计、probe、三仓独立安装包、服务停启及真实三组件隔离联调已完成本轮本地验收，见 [A 验收与交接](A-验收与交接-20261017.md)。本页前述待办是早期快照，以新记录逐项状态为准。正式 Nexus/Workbench 切换、远端 CI 与 Release 尚未执行，A 整体仍未准出；不得将测试服务或 dirty 本地包标为正式交付版本。


## 2026-10-17 独立候选与远端 CI

三个 A 候选已分别提交、推送并创建 Draft PR，最终提交的 GitHub CI 全部通过，见 [发布候选与 CI 验收](A-发布候选与CI验收-20261017.md)。Scout macOS/Windows、Nexus/Workbench macOS 原生构建均通过；候选发布步骤按设计跳过。正式 Release 和本机服务切换仍未执行，A 不标整体完成。
