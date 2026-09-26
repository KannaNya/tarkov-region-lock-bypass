# 架构

## 模块

```text
cli ── gui
 │
control ──────────── winapi.schtasks   (计划任务，schtasks.exe + XML)
 │                   winapi.kernel     (互斥体 / 停止事件 / 进程列表 / UAC)
keeper
 ├── softether ───── process           (vpncmd，唯一会启动子进程的地方)
 │        └───────── winapi.iphlpapi   (网卡地址、默认网关)
 ├── routes ──────── winapi.iphlpapi   (/32 路由、接口 metric)
 ├── game ────────── targets           (日志文件扫描)
 ├── targets                           (鉴权主机名 + DNS)
 └── relays ──────── catalog           (HTTPS CSV + VPNGate.dat)
```

| 模块 | 职责 |
| --- | --- |
| `config.py` | 读取 `config.json`（保留原有 PascalCase 键；已废弃的键会被忽略）。 |
| `catalog.py` | 解析 VPN Gate 官方 HTTPS 列表和 SoftEther 插件缓存 `VPNGate.dat`。 |
| `relays.py` | 候选排序（CIS 国家轮询、优先不同 IP）和失败冷却。 |
| `targets.py` | 从配置和近期 EFT 日志发现 lobby/gw-pvp/WSN 主机名，并在一个总时限内并发解析。 |
| `game.py` | 从 EFT 日志判断游戏阶段；日志尾部滚动或读取失败时不会解除已知的比赛保护。 |
| `softether.py` | 通过 vpncmd 配置/连接/断开专用账户；只有 SID 与有效 IPv4 租约同时存在才算连上。 |
| `routes.py` | 只管理本工具创建的 `/32` 路由，并把 VPN 网卡的接口 metric 固定为 9000。 |
| `keeper.py` | 后台循环：保护 → 维护 → 容错 → 切换节点。 |
| `control.py` | 组装 keeper；启动/停止/卸载计划任务；只读状态。 |
| `winapi/` | ctypes 绑定：iphlpapi、kernel32/shell32，以及 schtasks 包装。 |

## Keeper 循环

每一轮只做一件事：

1. **保护**：`PauseDuringRaid=true` 时，从匹配开始到结算结束，或者游戏在运行且节点已就绪，什么都不做，连只读的 vpncmd 查询也不发。
2. **维护**：会话正常时重新解析鉴权地址并同步 `/32` 路由。DNS 暂时没有结果时保留现有路由。
3. **容错**：已就绪的会话读不到时，先连续容忍 `SessionFailureThreshold` 次再切换。
4. **切换**：撤销路由并断开，读取节点目录，按顺序尝试候选。失败的节点冷却 `FailureCooldownMinutes` 分钟；SoftEther 本机资源忙（退出码 43）不算节点失败。一轮最多运行 `FailoverTimeoutSeconds` 秒，失败后按 `FailedCycleRetrySeconds` 指数退避，上限 `FailedCycleBackoffMaxSeconds`。

每条会改变状态的命令之前都会重新检查游戏阶段；切换进行到一半时开始匹配，下一条命令之前就会中止，不做任何清理。

## 进程间协作

- 后台进程在整个生命周期里持有互斥体 `Local\TarkovCIS-Keeper`；“是否在运行”就是一次 `OpenMutex`，没有会过期的 PID 文件。
- 后台进程创建事件 `Local\TarkovCIS-Stop`；`stop` 设置这个事件，后台自行撤销路由、断开 VPN 并退出。超时仍未退出时才结束计划任务，然后按 `routes.json` 离线清理。
- 状态写在 `%LOCALAPPDATA%\TarkovCIS\status.json`，日志写在 `keeper.log`（约 2 MiB，保留一个滚动备份）。

## 分流为什么可靠

- 鉴权 IP 各自有一条经 VPN 网关的 `/32` 路由，最长前缀匹配使它无论 metric 如何都优先于默认路由。
- VPN 网卡的接口 metric 固定为 9000。即使比赛中 SoftEther 的 DHCP 重新下发默认路由，它也排在物理网卡之后，普通流量不会变成全局代理。
- 添加路由前会确认物理网卡仍是默认出口，否则拒绝添加。
- 已存在的路由不会被认领，因此也永远不会被删除。

## 安全不变量

1. 不修改防火墙、DNS 或注册表；路由只写入活动存储（重启即消失）。
2. Raid 服务器 IP 永远不会进入 VPN 路由；日志里只读取主机名。
3. 除 vpncmd 和 schtasks 外不启动任何子进程；所有子进程都有超时并且不显示窗口。
4. 显式停止在比赛中也会清理；后台自动维护在比赛中绝不动手。
