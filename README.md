# Tarkov 俄区/CIS 区域锁分流与 VPN Gate 自动故障转移

这是一个面向 Windows 的开源工具，用于帮助在日本等非 CIS 网络环境中使用 CIS/俄区账号的 Escape from Tarkov 玩家，排查并改善区域锁、地区鉴权和二次授权连接问题。它建立“最小化分流”：只把已观察到的登录、区域鉴权和相关后端目标送入 SoftEther VPN Gate 的 CIS 出口，普通网络、更新下载以及独立 Raid 服务器继续走本地网络。

主程序已经重构为 Python 状态机和最小 Tkinter 图形界面；面向普通用户的 Windows 发布包包含独立 EXE，因此不要求预装 Python。旧 PowerShell 实现仍保留为兼容和回滚入口。项目不使用网上流传的固定 Tarkov IP 清单：它会重新解析目标域名，并从近期 EFT 日志中发现受限的 lobby/WSN 主机名；VPN Gate 中继失效时，会合并 SoftEther 原生节点目录、官方 HTTPS 列表和 48 小时内的本机成功记录，自动切换到下一个可用的 CIS 候选节点。

**关键词**：Escape from Tarkov（EFT）、Tarkov 俄区、CIS 区账号、区域锁/地区鉴权、二次授权、VPN Gate、SoftEther VPN、Windows、Python、最小化 split routing、自动换节点、VPN 故障转移、登录分流。

> 本项目是连接与分流辅助工具，不承诺绕过 BSG 的账号限制、地区政策或封禁机制；请确认使用方式符合游戏及 VPN 服务条款。

## 适合哪些人

- 在日本使用 CIS 区账号、遇到登录或地区验证失败的 EFT 玩家。
- 已安装 SoftEther VPN Client，并愿意使用 VPN Gate 公共中继的人。
- 希望只代理鉴权/后端目标，不想让整个游戏和普通上网流量走 VPN 的人。
- 想要后台常驻、节点下线后自动寻找下一个 CIS 节点的人。
- 希望双击图形界面完成启动、停止和卸载，不想维护一大段 PowerShell 脚本的人。

## 不适合哪些情况

- 需要全局 VPN 加速、降低 Raid 延迟或隐藏全部公网流量的人。
- 没有 SoftEther VPN Client 或不接受公共 VPN 中继波动的人。
- 期待项目保证绕过 BSG 账号限制、地区政策或封禁机制的人。本项目不提供这种保证，请遵守游戏和 VPN 服务条款。

## 工作方式

1. Python 目录读取器使用 SoftEther VPN Gate 插件显示的同一份节点列表 `VPNGate.dat`，分别读取 SoftEther TCP 的 `SslPorts` 和 UDP NAT-T 的 `UdpPort`；它只确认文件格式和签名标记存在，不把这描述为密码学验签。
2. 同时查询 VPN Gate 官方 HTTPS/OpenVPN 列表作为新鲜的第二数据源，再按 `RU → UA → 其他配置中的 CIS 候选` 排序并去重。
3. 对明确声明 TCP 端口的节点使用 TCP；对插件列出的 UDP NAT-T 端口使用 SoftEther 自身的 UDP 穿透连接。OpenVPN UDP 端口不会被当成 SoftEther 端口。这样 `SslPorts` 为空但 `UdpPort` 有值的节点也能参与切换。
4. 将候选节点写入 SoftEther 账户 `Tarkov-CIS-PlayOnly`，并关闭 SoftEther 自身的无限重试。只有 `SID-*` 会话、正常虚拟网卡、非 APIPA IPv4 和 VPN 网关同时存在才会报告连接成功。
5. Python Keeper 由显式状态机驱动，常驻任务持续检查会话、网卡、IPv4 地址、临时路由和普通默认出口。
6. 当前节点失效时，删除本工具管理的旧路由并尝试下一个；一批候选均失败后每 10 秒刷新目录并重试，全部处于冷却期时每轮最多复测 3 个最早失败的独立中继。SoftEther 错误 43 属于本机资源忙，不会误伤远端候选节点。
7. 默认 `DisconnectAtRaid=true`：登录、选角色、大厅和匹配阶段保持 SoftEther 连接与鉴权分流；只有本机游戏日志记录带时间戳的 `GameStarted:`，确认已经进入 Raid 后，才撤销本工具的 `/32` 路由并断开 SoftEther。当前游戏会话内不再重连，包括战局结算阶段；`UserMatchOver` 表示开始结算，不表示已经回到大厅。若显式设为 `false`，则使用旧的匹配/Raid 保持会话保护模式。旧配置项 `DisconnectAtMenu` 不再生效。
8. 一轮故障转移默认最多运行 180 秒，并优先尝试不同 IP 的独立中继；候选按 CIS 国家轮询，先保证 RU 后的 UA/KZ/BY 等国家各有机会，再补齐同一国家的其他节点；同一中继的其他 SSL 端口只作为后备，不会挤占全部候选名额。
9. 目标域名解析出的地址使用临时 `/32` 路由走 VPN；VPN 默认路由提高 metric，因此日本物理网卡仍是普通流量的默认出口。

成功建立过真实会话的节点会在本机保留 48 小时作为短期备用；成功后也会立即清除该 endpoint 的失败冷却。该记录位于 `%LOCALAPPDATA%\TarkovCIS`，不会作为固定公网 IP 上传到仓库；这可应对两个实时目录暂时不可用或漏掉仍可用节点的情况。

原生目录默认只接受 24 小时内的缓存。打开 SoftEther 的“VPN Gate 公共 VPN 中继服务器”并刷新列表，会由官方插件更新该文件；如果文件不存在或过期，工具仍会自动使用 HTTPS 数据源和近期验证成功的节点。原生服务端目录通过 HTTP 分发并附带签名，因此本项目目前不会绕过官方插件直接下载并信任未经校验的原生响应。

Windows 路由按目标 IP 选择，不能按 URL 路径或进程区分流量。如果登录、匹配或其他 HTTPS 服务共享同一个 CDN/IP，它们无法用普通静态路由进一步拆分。独立 Raid 服务器不会因为本项目的已知目标列表而被加入 VPN，但仍应结合本机日志复核实际服务器。

## 环境要求

- Windows 10/11。
- SoftEther VPN Client，且已创建并启用 `VPN - VPN Client` 虚拟网卡。
- 使用发布包时无需安装 Python；从源码运行需要 Python 3.11 以上。
- Windows PowerShell 5.1 只承担安装/迁移、计划任务控制和 UAC 薄包装；常驻 Keeper 任务直接运行 Python，不依赖 PowerShell 作为长期进程。
- 启动界面时接受管理员权限提示（计划任务和临时路由需要）。
- 可选：EFT 本地日志目录，用于动态发现 lobby/WSN 主机名。

## 图形界面（推荐）

1. 从 GitHub Actions/Release 取得 `TarkovCIS-Windows.zip` 并完整解压；从源码运行也可以直接使用仓库目录。
2. 安装并启用 SoftEther VPN Client 的 `VPN - VPN Client` 虚拟网卡。
3. 双击 `Tarkov-CIS-GUI.cmd`（也可直接双击 `TarkovCIS.exe`），接受一次 Windows 管理员权限提示。
4. 点击“启动”。第一次会生成本机 `config.json`、安装计划任务，并在确认后台 Keeper 已写出同世代心跳后返回；它不会等待 VPN 节点连接完成，连接与换节点会继续在后台进行。

从源码目录运行时请使用 `Tarkov-CIS-GUI.cmd`：它会优先启动当前 Python 源码，避免误用本机 `dist` 目录中以前构建的 EXE。发布包仍直接使用其中的 `TarkovCIS.exe`。

界面会每 3 秒刷新任务、VPN、核心状态和日志，并提供以下操作：

- **启动**：确认后台进程和新鲜心跳后返回，不等待节点连通；重复点击不会启动第二个 Keeper。检测到本项目旧 PowerShell 任务时，会先让旧任务清理其自有路由，再原位升级。
- **停止分流**：停止后台任务、删除临时路由并断开 VPN。
- **卸载后台任务**：移除计划任务、删除临时路由并断开 VPN。

关闭 GUI 不会停止后台任务。首次启动会根据 `config.example.json` 自动创建本机专用的 `config.json`；该文件已被 Git 忽略。静态鉴权域名可直接工作；若要增加基于本机证据的 WSN 动态发现，可把 `GameLogRoots` 填为实际 `EscapeFromTarkov\Logs` 目录后重新启动任务。

进入 Raid 后的断开依赖本机 EFT 日志中的 `GameStarted:` 标记；状态栏显示 `raid_direct` 才表示后台已确认撤销路由并断开 VPN。公共 VPN Gate 候选全都不可达时，10 秒重试也不能保证建立连接。

## 命令行方式

发布包中的 EXE：

```powershell
.\TarkovCIS.exe start
.\TarkovCIS.exe status
.\TarkovCIS.exe candidates
.\TarkovCIS.exe stop
.\TarkovCIS.exe uninstall
```

从源码运行无需全局安装包：

```powershell
py -3 .\Tarkov-CIS-Python.py start
py -3 .\Tarkov-CIS-Python.py status
py -3 .\Tarkov-CIS-Python.py candidates
py -3 .\Tarkov-CIS-Python.py stop
```

`start` 只负责安装任务并确认后台 Keeper 的新世代心跳，不等待 VPN 连通；前台调试才使用 `run`。`status` 和 `candidates` 是只读命令，不建立 VPN、不写失败冷却；其中 `candidates` 只展示普通非冷却候选，不会触发“全部冷却后的受控回退”。

直接使用命令行执行 `start`、`stop`、`uninstall` 或 `run` 时，请先打开“以管理员身份运行”的终端；图形界面会自行触发 UAC。`status` 和 `candidates` 保持只读。

首次连接优先尝试 SoftEther 插件列表中的俄罗斯节点，支持 TCP 和 UDP NAT-T；若俄罗斯节点没有完成 SID 和租约验证，会继续乌克兰及其他 CIS 国家。掉线后后台按配置刷新目录；一整批候选失败时，默认 10 秒后取新快照。若当前目录内所有 endpoint 都处于 15 分钟冷却，每轮最多复测 3 个最早失败的独立中继。

## 日常管理

```powershell
# 查看计划任务、Python Keeper、核心状态、VPN 租约和路由数量
py -3 .\Tarkov-CIS-Python.py status

# 查看当前常规候选，不进行连接
py -3 .\Tarkov-CIS-Python.py candidates

# 停止后台任务、撤销精确 owned 路由、恢复 VPN 默认路由 metric 并断开账户
py -3 .\Tarkov-CIS-Python.py stop

# 删除计划任务；配置、KnownGood 和日志仍保留
py -3 .\Tarkov-CIS-Python.py uninstall
```

运行时数据位于 `%LOCALAPPDATA%\TarkovCIS`：

- `keeper.log` 与 `keeper.log.1`：最多约 2 MiB 加一个滚动备份。
- `failures.json`：最近两天的 endpoint 级失败冷却。
- `known-good.json`：48 小时内真实连接成功的短期备用。
- `routes.json`：本工具精确拥有的 `(目标 /32, ifIndex, gateway)` 和待恢复默认 metric。
- `keeper.pid.json` / `keeper-status.json`：进程控制和无敏感字段的状态快照。

旧 PowerShell 实现仍位于 `src/`。Python 入口识别到同名旧任务时可安全迁移；需要回滚时也可继续使用原来的 `Connect-VpnGateCis.ps1` 和 `Tarkov-CisRouteKeeper.ps1`。不要让新旧两个 Keeper 同时管理同一个 SoftEther 账户。

## 如何确认分流正确

```powershell
# 确认 VPN 账户当前使用的节点
& 'C:\Program Files\SoftEther VPN Client\vpncmd_x64.exe' /CLIENT localhost /CMD AccountList

# 查看默认路由、VPN 接口和临时目标路由
Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0'
Get-NetAdapter -Name 'VPN - VPN Client'
Get-NetRoute -AddressFamily IPv4 | Where-Object DestinationPrefix -like '*/32'

# 普通网站应仍显示日本出口
(Invoke-RestMethod 'https://ifconfig.co/json').country_iso
```

预期结果是：日本以太网仍然是默认路由；只有本工具管理的目标 `/32` 路由指向 SoftEther 虚拟网卡；独立 Raid 目标没有被添加到 VPN。

## 安全与隐私

- 不修改防火墙、DNS、注册表或永久系统路由。
- 路由写入 `ActiveStore`，可通过 `Stop` 或 `Uninstall` 撤销。
- 不要提交 `config.json`、账号密码、VPN 会话密钥、EFT 日志或状态文件。
- VPN Gate 是公共中继，节点可能随时离线、变更出口或性能波动；自动故障转移只能提高可用性，不能保证每个节点都能通过游戏验证。

## 项目状态与限制

旧 PowerShell 流程已经在 Windows + SoftEther VPN Gate 上完成过端到端验证。新的 Python 版本目前是 Beta：核心、Windows 适配器、GUI 控制、任务包装和旧版回归均有自动化测试，本机也已只读验证能够解析当前 SoftEther 原生目录、识别真实 SID/租约和列出当下 RU/UA 候选；本次重构没有自动替换正在运行的旧任务，也没有在测试过程中写路由。正式切换前仍应做一次人工可观察的 `start → 登录/二次授权 → stop` 验收。

BSG 后端、DNS/CDN 和 VPN Gate 列表会变化，提交新问题或贡献时请附上新鲜的本机日志、任务状态和路由证据，不要复制过时的 IP 清单。普通 Windows 目标路由不能按 URL、进程或端口拆分共享 IP；本项目保证的是“只添加当前鉴权目标的 `/32`”，不是对任意共享 CDN 地址的进程级隔离。
