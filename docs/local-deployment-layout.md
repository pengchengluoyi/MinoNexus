# Nexus + Workbench 部署目录

macOS 的完整产品由一个 Python wheel 提供，一个 launchd 服务监听 50104。Workbench 是包内 `mino_nexus/web/` 的静态文件，没有独立 60104 服务。

- 业务数据：`~/Library/Application Support/Mino/nexus/data/`。
- 服务定义：`~/Library/LaunchAgents/com.mino.nexus.plist`。
- 日志：`~/Library/Logs/Mino/nexus/nexus.log` 与 `nexus-error.log`。
- Python 运行环境：由 pipx 管理，服务引用安装时的解释器路径。
- 开发环境：`~/.mino-dev/nexus`、Nexus 50101、Vite 60101；禁止与生产数据根重叠。
- Docker：同一 wheel，非 root UID 10001，数据目录 `/data` 挂 named volume，宿主发布 50104；默认关闭 mDNS。

`mino-nexus service install/start/stop/restart/status` 只支持 macOS。重复 install 若配置相同可复用；配置不同则拒绝覆盖并要求人工审阅。生产启动必须显式使用 `--prod`，普通源码启动保持开发默认值。

Scout 仍在设备所在机器独立安装，数据、配置和进程归 Scout 管理，不放进 Nexus 容器。

旧 `~/Mino/…` 或独立 Workbench 安装目录不自动移动、删除或合并。迁移必须先停止写入、备份、复制到新目录并显式指定新路径，原目录留作回滚。B4 自动备份暂缓，当前版本不会替代人工备份。

详见 [发布与升级](github-updates.md) 及 [合并部署方案](架构/V7/Nexus-Workbench合并部署方案.md)。
