# Scout 进程与「生产实例」的边界（2026-10-23 实测记录）

起因：用**正式环境 MCP（50104）**去重启**测试环境的 “nexus dev”**，结果仍报
`awaiting_developer / ide_controller_restart / 目标配置运行着当前执行节点`。
追这条线时先说清一个**容易被误判的事实**：

> 本机同时存在**两个 Scout**，它们跑的是**同一个程序（同一套 `mino_scout` 代码）**，只是入口不同：
> 一个是 launchd 托管的打包版（连正式 50104），一个是工作树源码起的 `python -m mino_scout.cli run`（连测试 50101）。
> 「同一个程序」指的是代码同源、行为同构 —— 改工作树不会影响打包版，反之亦然。

## 1. 实测事实

| 检查 | 打包版 Scout（正式） | 源码 Scout（测试） |
|---|---|---|
| 进程 | `~/Library/Application Support/Mino/scout/bin/mino-scout run`（10.2 MB 单文件） | `python -m mino_scout.cli run`（Framework Python 3.12） |
| 托管 | launchd `com.mino.scout`（pid 32240） | PyCharm 运行配置 “Scout Development” |
| 数据目录 | `~/Library/Application Support/Mino/scout` | `~/.mino-dev/scout` |
| `config.json` → `nexus_url` | `http://192.168.190.84:50104`（**正式**） | `http://localhost:50101`（**测试**） |
| 报告 `node_id` | `e22615fbbb917111` | 同值（node_id 由机器身份派生，不是 scout_id） |
| 库 | 容器卷 `mino-production_mino-data` | `~/.mino-dev/nexus/mino.db` |

关键点：**两个 Scout 的 `node_id` 相同**（由机器身份派生），所以两侧 `scout_list` 看起来是同一台节点、
同一套设备、同一份开发绑定 —— 很容易误判成“只有一个 Scout”。实际是两条独立连接。

也就是说：

- **测试环境 50101**：源码 Scout（工作树代码）+ `~/.mino-dev/nexus/mino.db`；
- **正式环境 50104**：容器 + 打包版 Scout（launchd）+ 独立卷。

两边都真的在线，互不影响。**杀掉源码 Scout 不会让正式节点下线**——这就是“正式环境节点仍在线”的原因。

## 2. 由此产生的三个坑

1. **同名同码、两套入口，操作落到哪个 Scout 取决于你连哪个 Nexus**
   - 连 50104 → 打包版 Scout 执行；
   - 连 50101 → 源码 Scout 执行。
   两边共用同一个 PyCharm 项目与同一份开发绑定，所以谁都能对同一批 Run Configuration 发指令。
2. **`ide_controller_restart` 的自我拒绝会被误判为权限问题（两个 Scout 都会有）**
   它其实是 Scout 侧的保护：目标配置里有进程等于/包含当前 Scout 进程时拒绝停机。
   由于 `inspect_running()` 用 pid 观测，Scout 与目标配置同目录运行时会**误认成同一实例**，
   把正常重启也拒掉（已按 2026-10-23 决定移除该拒绝，停配置按 `sessionId` 定位，与 pid 无关）。
3. **`runtime_environment: production` 不能当作「连的是生产」**
   它是 Scout 自身的运行环境标记，与 `nexus_url` 无关。判断连谁只看 `scout.config.json` 的 `nexus_url`。

## 3. 判断方法（以后照这个查，不要靠猜）

```bash
# 1) 两个 Scout 各连哪个 Nexus（两个都要看，只看一个必误判）
ps -eo pid,ppid,lstart,command | grep -Ei "[m]ino_scout\.cli|Mino/scout/bin/mino-scout"
launchctl print gui/$(id -u)/com.mino.scout 2>/dev/null | grep -E "program|pid|state"
cat ~/.mino-dev/scout/config.json                | grep nexus_url   # 源码版
cat "$HOME/Library/Application Support/Mino/scout/config.json" | grep nexus_url   # 打包版

# 2) 两个 Nexus 是不是同一份数据
curl -s http://127.0.0.1:50101/health | head -c 120     # 测试
curl -s http://192.168.190.84:50104/health | head -c 120 # 正式（容器）

# 3) 节点在线是谁撑起来的
#    两侧 scout_list 的 node_id 相同（机器身份派生），不能靠 node_id 区分；
#    用 lsof 看谁真的连着 50104：
lsof -nP -iTCP:50104 | grep ESTABLISHED
```

## 4. 结论与约定

- 排查设备/跑批问题时**先确认自己连的是哪个 Nexus**（50101 测试 / 50104 正式），
  两边各有自己的 Scout，日志和库都不是同一份。
- 改 Scout 代码只影响**源码版**；打包版要重新打包/更新才会变。判断依据看进程 cmdline，
  不是看 `scout_list` 的版本号（两个 Scout 报告同一个 `node_id`）。
- 开发联调统一用 50101；50104 只做正式验证，不要用它去重启测试环境的 Run Configuration
  （它的打包版 Scout 跑的仍是线上代码，行为与工作树不一致）。
