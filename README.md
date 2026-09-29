# Tarkov 俄区/CIS 区域锁分流与 VPN Gate 自动故障转移

这是一个面向 Windows 的开源工具，用于帮助在日本等非 CIS 网络环境中使用 CIS/俄区账号的 Escape from Tarkov 玩家，排查并改善区域锁、地区鉴权和二次授权连接问题。它建立“最小化分流”：只把登录、区域鉴权相关的后端地址送入 SoftEther VPN Gate 的 CIS 出口，普通网络、更新下载以及独立 Raid 服务器继续走本地网络。

程序由 Python 编写，带一个最小的 Tkinter 图形界面；面向普通用户的 Windows 发布包包含独立 EXE，不要求预装 Python。项目不使用网上流传的固定 Tarkov IP 清单：它每次重新解析目标域名，并从近期 EFT 日志中发现 lobby/WSN 主机名；VPN Gate 节点失效时，会合并 SoftEther 原生节点目录和官方 HTTPS 列表，自动切换到下一个可用的 CIS 节点。

**关键词**：Escape from Tarkov（EFT）、Tarkov 俄区、CIS 区账号、区域锁/地区鉴权、二次授权、VPN Gate、SoftEther VPN、Windows、Python、最小化 split routing、自动换节点、VPN 故障转移、登录分流。

> 本项目是连接与分流辅助工具，不承诺绕过 BSG 的账号限制、地区政策或封禁机制；请确认使用方式符合游戏及 VPN 服务条款。

## 适合哪些人

- 在日本使用 CIS 区账号、遇到登录或地区验证失败的 EFT 玩家。
- 已安装 SoftEther VPN Client，并愿意使用 VPN Gate 公共中继的人。
- 希望只代理鉴权目标，不想让整个游戏和普通上网流量走 VPN 的人。
- 想要后台常驻、节点下线后自动寻找下一个 CIS 节点的人。

## 不适合哪些情况

- 需要全局 VPN 加速、降低 Raid 延迟或隐藏全部公网流量的人。
- 没有 SoftEther VPN Client 或不接受公共 VPN 中继波动的人。
- 期待项目保证绕过 BSG 账号限制、地区政策或封禁机制的人。

## 工作方式

1. **节点目录**：每轮切换时直接从 SoftEther VPN Gate 插件使用的同一个接口下载完整列表 `VPNGate.dat`（包含 TCP 的 `SslPorts` 和 UDP NAT-T 的 `UdpPort`，节点远多于 HTTPS 列表），同时下载 VPN Gate 官方 HTTPS 列表；只保留 CIS 国家的节点。下载失败时使用上一次的缓存或插件自己保存的文件。原生列表只确认格式和签名字段存在，不做密码学验签。
2. **候选排序**：48 小时内真正连通过的节点最先尝试（即使它暂时不在目录里）；然后按 `RU → UA → 其他 CIS 国家` 轮询，同一国家内优先报告带宽高、延迟低的节点；先尝试不同 IP，同一中继的其他端口放在最后。
3. **连接**：把候选写入专用 SoftEther 账户 `Tarkov-CIS-PlayOnly` 并关闭 SoftEther 自身的无限重试。只有出现 `SID-*` 会话、并且虚拟网卡拿到非 APIPA 的 IPv4 地址和网关，才算连上。
4. **分流**：鉴权域名解析出的每个地址添加一条经 VPN 的临时 `/32` 路由；VPN 网卡的接口 metric 固定为 9000，因此 SoftEther 下发的默认路由永远排在物理网卡后面，普通流量不会变成全局代理。
5. **故障转移**：已就绪的会话连续 3 次读不到时切换节点；失败的节点冷却 15 分钟，一轮最多 180 秒，一整批都失败后每 10 秒重试。SoftEther 本机资源忙（退出码 43）不算节点失败。
6. **游戏保护**：从匹配开始到战局结算结束，或者游戏在运行且节点已就绪时，后台完全不碰 VPN 和路由，连只读的 SoftEther 查询也不发；切换进行到一半时开始匹配，会在下一条命令之前中止。只有鉴权目标的 `/32` 走 VPN，Raid 服务器和默认网络仍走本地连接。

Windows 路由按目标 IP 选择，不能按 URL 路径或进程区分流量。如果登录与其他 HTTPS 服务共享同一个 CDN/IP，它们无法用静态路由进一步拆分。

原生列表是明文 HTTP 下载的（和官方插件一样），只接受 24 小时内的数据；不需要手动打开 SoftEther 的 VPN Gate 插件。

## 环境要求

- Windows 10/11。
- SoftEther VPN Client，且已创建名为 `VPN` 的虚拟网卡（系统中显示为 `VPN - VPN Client`）。
- 使用发布包时无需安装 Python；从源码运行需要 Python 3.11 以上。
- 启动界面时接受管理员权限提示（计划任务和临时路由需要）。
- 可选：EFT 本地日志目录（`GameLogRoots`），用于识别游戏阶段和动态发现 WSN 主机名。

## 图形界面（推荐）

1. 从 GitHub Actions/Release 取得 `TarkovCIS-Windows.zip` 并完整解压；从源码运行也可以直接使用仓库目录。
2. 安装 SoftEther VPN Client，并创建 `VPN` 虚拟网卡。
3. 双击 `Tarkov-CIS-GUI.cmd`（也可直接双击 `TarkovCIS.exe`），接受管理员权限提示。
4. 点击“启动”。第一次会根据 `config.example.json` 生成本机 `config.json`、注册计划任务，并在确认后台进程启动后返回；节点连接和切换在后台继续进行。

界面每 3 秒刷新一次状态和日志：

- **启动**：注册并启动后台任务；重复点击不会启动第二个后台进程。
- **停止**：通知后台撤销临时路由、断开 VPN 并退出。
- **卸载后台任务**：停止后删除计划任务；配置和日志保留。

关闭界面不会停止后台任务。`config.json` 已被 Git 忽略；把 `GameLogRoots` 改成实际的 `EscapeFromTarkov\Logs` 目录后重新启动任务，即可启用游戏阶段识别。

## 命令行

发布包中的 EXE：

```powershell
.\TarkovCIS.exe start
.\TarkovCIS.exe status
.\TarkovCIS.exe candidates
.\TarkovCIS.exe stop
.\TarkovCIS.exe uninstall
```

从源码运行（无需安装包）：

```powershell
py -3 .\Tarkov-CIS-Python.py start
py -3 .\Tarkov-CIS-Python.py status
py -3 .\Tarkov-CIS-Python.py candidates
py -3 .\Tarkov-CIS-Python.py stop
```

`start`、`stop`、`uninstall` 需要在“以管理员身份运行”的终端中执行；`status` 和 `candidates` 是只读的，不连接 VPN、不改路由。`run` 在前台运行后台循环，用于调试（同样需要管理员权限）。

## 运行时数据

位于 `%LOCALAPPDATA%\TarkovCIS`：

- `keeper.log` 与 `keeper.log.1`：最多约 2 MiB 加一个滚动备份。
- `status.json`：后台当前状态。
- `routes.json`：本工具创建的 `(目标 /32, 接口, 网关)`，后台异常退出后仍可精确清理。
- `known-good.json`：48 小时内真正连通过的节点（`KnownGoodLifetimeHours`）。
- `VPNGate.dat`：最近一次下载的完整节点列表。

## 如何确认分流正确

```powershell
# 默认路由：物理网卡应排在 VPN 网卡前面
Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0'
Get-NetIPInterface -InterfaceAlias 'VPN - VPN Client' -AddressFamily IPv4

# 本工具添加的 /32 路由
Get-NetRoute -AddressFamily IPv4 -InterfaceAlias 'VPN - VPN Client' | Where-Object DestinationPrefix -like '*/32'

# 普通网站应仍显示日本出口
(Invoke-RestMethod 'https://ifconfig.co/json').country_iso
```

## 安全与隐私

- 不修改防火墙、DNS 或注册表；路由只写入活动存储，重启即消失，也可以通过“停止”撤销。唯一的持久改动是把 VPN 网卡的接口 metric 设为 9000。
- 不要提交 `config.json`、账号密码、VPN 会话密钥、EFT 日志或状态文件。
- VPN Gate 是公共中继，节点可能随时离线、变更出口或性能波动；自动故障转移只能提高可用性，不能保证每个节点都能通过游戏验证。

## 项目状态与限制

BSG 后端、DNS/CDN 和 VPN Gate 列表会变化，提交问题或贡献时请附上新鲜的本机日志、状态和路由证据，不要复制过时的 IP 清单。普通 Windows 路由不能按 URL、进程或端口拆分共享 IP；本项目保证的是“只添加当前鉴权目标的 `/32`”，不是对共享 CDN 地址的进程级隔离。

架构说明见 [docs/architecture.md](docs/architecture.md)。
