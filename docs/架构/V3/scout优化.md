# Scout 优化

> **状态**：已按本方案落地。心跳带 `host`；网页「休眠 / 启动」走 `node.sleep` / `node.wake`。进程退出仍是 `node.stop`。  
> **现象**：本机开着 Scout 时插电充不进去，关掉 Scout 进程后能充。Studio 网页上这台节点已是离线（`e22615fbbb917111`，v0.1.54，心跳停在约 5 小时前），操作列没有「启动」。  
> **关联**：[协议与节点生命周期](../../scout/04-协议与节点生命周期.md)、[Studio 与运维](../../scout/05-Studio与运维.md)。

两条是一件事的两头。网页上的「停止」把进程退掉了，机器才充得上电；进程退掉之后，网页又没有办法把它拉起来。

---

## 1. 现在实际发生了什么

### 1.1 功耗

Scout 一连上 Nexus 就持有睡眠抑制，不看有没有任务在跑。macOS 上是：

```text
caffeinate -d -i -m -s -w <scout pid>
```

`-d` 让屏幕不熄，注释里写的原因是不少 Mac 熄屏后 USB 掉电。`-s` 在插电时还压住合盖睡眠。实现在 `mino_scout/power.py`，挂在 `NodeTransport.run_forever` 上，进程退出才放开。

屏幕常亮，再加上有任务时的 Chromium（headed，1280×800），整机功耗可以高于充电器能送进来的功率。电池百分比就不涨。关掉 Scout，`caffeinate` 和浏览器一起没了，充电器才盖得住。这是目前最像的原因。监控要拿数字确认，不能只靠这次体感。

### 1.2 网页停止之后

网页 Scout 列表里，远程节点的「停止」走 `POST /runtime/nodes/{id}/command`，body 是 `stop`。Nexus 下发 `node.stop`。Scout 回完 RESULT 后进程退出。

launchd 的 `KeepAlive` 只在 **崩溃** 时拉起（`packaging/install.sh` 里 `Crashed=true`）。正常退出不会再起来。

同一张表里，远程节点的「启动」是藏起来的。`scoutNodes.js` 里原因写的是「离线专机无法远程启动」。Nexus 对 `command=start` 直接 400，同一句文案。进程已经不在了，Nexus 的 WebSocket 也到不了那台电脑，网页没有第二条通道。

所以截图里那一行是：状态离线、心跳停住、操作列没有启动。桌面版 Studio 只对本机那一行有 IPC 启动；浏览器页面没有。

---

## 2. 性能功耗监控

采样挂在现有心跳上，15 秒一次。不新开采集进程，不用 `powermetrics`（要 root）。

每次心跳带一个 `host` 对象：

| 字段 | 从哪读 | 给谁看 |
|------|--------|--------|
| `power_source` | `pmset -g batt`：`AC Power` / `Battery Power` | 插电还是电池 |
| `charging` | 同一条输出里的 charging / not charging / charged | 插着电却不在充，就是这次的现象 |
| `battery_percent` | 同上 | 电量 |
| `inhibit` | 已有 `PowerGuard.status()`：是否持有、`caffeinate` 是否还活着 | 屏幕抑制开没开 |
| `cpu_percent` | Scout 自己的进程 CPU | 是不是 Scout 在吃 CPU |
| `rss_mb` | Scout 自己的常驻内存 | |
| `children` | 子进程按名字计数：`Chromium` / `caffeinate` / `adb` | 浏览器开了几个 |

Studio 列表在心跳列旁边加一行，在线节点才有数。插电且 `charging=not charging` 时标成警告，并带上当时的 `inhibit` 和 Chromium 个数。离线节点没有采样，不要显示 0。

`Heartbeat` 今天没有这个对象。加上 `host` 是协议字段：两仓 `protocol.py` 和 golden fixture 同一轮改。旧 Scout 不带该字段时，Studio 这一格显示「—」。

这一步只展示，不改 `caffeinate` 的参数。

---

## 3. 网页停止之后如何再启动

网页上的「停止」改成休眠，不退出进程。进程还在，心跳还在，Nexus 才下得了「启动」。

休眠时 Scout 做三件事：

1. `PowerGuard.release`，`caffeinate` 退出，屏幕可以熄，充电不再被整机功耗顶住。
2. 关掉这个节点上的 Chromium / Playwright context。没有任务时本来也不该留着浏览器。
3. 心跳继续。`host.mode=asleep`。列表状态显示「已休眠」，原来的「停止」换成「启动」。

启动是心跳还在时的一条指令：重新 `acquire` 睡眠抑制。浏览器不在启动时开，下次任务要页面时再开。

| 操作 | 谁能点 | 进程 | 之后网页能启动吗 |
|------|--------|------|------------------|
| 休眠（网页「停止」改成这个） | 网页、桌面 | 还在，心跳在 | 能，同一行「启动」 |
| 启动 | 节点仍连着时 | 还在 | — |
| 退出进程 | 只留本机 `mino-scout stop` 和桌面版 IPC | 没了 | 不能。网页继续藏起远程「启动」 |

`node.stop` 的含义不要改成休眠。退出和休眠分成两条指令：`node.sleep`、`node.wake`。网页按钮调用休眠；真正退出仍走现在的 `node.stop`，避免桌面版「停止」悄悄变成还留着进程。

离线行保持今天的行为：没有启动按钮。行上写本机命令 `mino-scout start`（内部是 `launchctl kickstart`）。不要从 Nexus 去唤醒一台已经没有进程的电脑。

---

## 4. 空闲时放开屏幕常亮：先不做

没有任务时去掉 `caffeinate -d`、让屏幕可以熄，这一档先不做。

要等 Studio 功耗列里出现「插电、屏幕常亮、未充电」的数字之后，再决定要不要做。在那之前不改 `caffeinate` 的参数。
