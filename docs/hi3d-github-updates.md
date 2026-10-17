# Nexus / Workbench GitHub 更新

仅 macOS 独立安装基线；源码启动实例拒绝自更新。A 阶段旧包没有维护合同，首次应在新目录安装 D2/D3 基线，不覆盖旧目录或直接替换业务库。

## 固定版本流程

1. 管理员通过 `nexus_update_check` 或 `workbench_update_check` 查询配置仓库的最新稳定 Release；可传固定 `release_tag`。检查不写安装、不下载、不触发迁移。
2. 保留返回的 `release_tag` 和 `target_build_id`。使用 `nexus_update_prepare` / `workbench_update_prepare` 下载这一对固定标识；不能传任意 URL，不能以 `latest` 执行下载。
3. 校验发布 tag/version、component、平台、source_commit、build、包大小、SHA256 和原生包合同。失败的下载保留现场；同一 build 已成功准备时复用，不覆盖旧包。
4. 单独调用 `nexus_update` / `workbench_update`，提供稳定的 `operation_id` 与固定 `build_id`。下载成功不代表兼容性通过。
5. 按原 operation 查询；断线或助手启动回执丢失，不新建操作重试。仅等待状态允许取消。

Nexus 准入要求 `config/compatibility/<from_build>--<to_build>.json` 的真实演练记录，绑定两份包的 SHA256，验证迁移重复运行、旧版启动读写、写者排空与迁移外部副作用。不能手工填 true 代替验证，也不把空库演练当成生产库兼容。

Workbench 准入要求包声明已验证的 Nexus build，且现场 `/sys/server_info` 与声明匹配；无声明、脏包、实例不符均拒绝。首次安装可先部署，更新时才进行组合准入。需要人工恢复的旧操作会阻止下一次更新。

## 本机安装入口

使用包中 `launcher/mino-maintain`：

```sh
mino-maintain d2 install --root /absolute/new/nexus --data-dir /absolute/new/nexus/data --manifest /absolute/manifest.json --package /absolute/package.zip --host 127.0.0.1 --port 50101
mino-maintain d2 provision-probe --root /absolute/new/nexus
mino-maintain d2 register --root /absolute/new/nexus
mino-maintain d2 check-update --root /absolute/new/nexus
mino-maintain d2 fetch --root /absolute/new/nexus --tag vX.Y.Z --build EXACT_BUILD
```

这些是参数示例，不能直接复制占位路径执行。安装后的稳定入口位于安装根的 `launcher/mino-maintain`。

Workbench 安装后，使用 Nexus 维护入口执行：

```sh
mino-maintain d2 bind-workbench --root /absolute/nexus --workbench-root /absolute/workbench
```

绑定写入 Nexus 的部署配置，HTTP/MCP 调用者不能指定本机路径。支持同机一个已登记 Workbench；跨主机更新未实现。Workbench 的检查/准备也可直接用其维护入口 `github-check --root ROOT` / `github-fetch --root ROOT --tag TAG --build BUILD`。

## 执行与恢复

Nexus：持久维护门禁 → 等待任务与实际写者结束 → 优雅停服 → 受管数据备份 → 同源离线迁移 → 原子版本切换 → 正常启动 → health、实例/build、只读 MCP 探测。新版已经开始启动后只做兼容代码回退，不自动覆盖业务库。无法证明安全时保持 `needs_operator`，由本机 `d2 recover` 按现场阶段处理。

Workbench：相同运行时的静态包可热切换；运行时变化需要已登记服务停启。旧资源 URL 保留，验证实际 build/runtime/installation 后完成；失败尝试恢复原版本，恢复失败保留人工处理状态。

GitHub Actions 在 main 上构建并上传 native-package 产物和 manifest；只有版本 tag 工作流会发布 Release。下载 Actions 产物不等于已发布 Release。未发布 manifest 时，版本检查明确报发布物缺失。

维护助手稳定保留在 launcher；当前不自动升级助手自身。升级应用不能被宣传为助手修复已自动生效。未来助手合同不兼容时应安排独立基线迁移。

## 固定环境端口

Nexus 测试 50101、正式 50104；Workbench 测试 60101、正式 60104。测试安装使用 `--environment test`，正式默认 `production`；Nexus 源码 `--dev` 默认 50101。Vite 开发服务器使用 60101 并禁止端口占用时自动换号。Scout 主动连接对应 Nexus，不分配独立业务监听端口。既有配置不自动重写，迁移须检查现有进程与数据根。
