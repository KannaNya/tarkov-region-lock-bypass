# Tarkov 区域锁分流与 VPN Gate 自动故障转移

这是一个面向 Windows 的 PowerShell 工具，用于帮助在日本网络环境中使用 CIS 区账号的 Escape from Tarkov 玩家，建立“最小化分流”连接：只把已观察到的登录、区域鉴权和相关后端目标送入 SoftEther VPN Gate 的 CIS 出口，普通日本网络、更新下载以及独立 Raid 服务器继续走日本本地网络。

项目不依赖 Python，也不使用网上流传的固定 Tarkov IP 清单。它会定期重新解析目标域名，并从近期 EFT 日志中发现 lobby/WSN 主机名；VPN Gate 中继失效时，会合并 SoftEther 原生节点目录与官方 HTTPS 列表，自动测试并切换到下一个可用的 CIS 候选节点。

## 适合哪些人

- 在日本使用 CIS 区账号、遇到登录或地区验证失败的 EFT 玩家。
- 已安装 SoftEther VPN Client，并愿意使用 VPN Gate 公共中继的人。
- 希望只代理鉴权/后端目标，不想让整个游戏和普通上网流量走 VPN 的人。
- 想要后台常驻、节点下线后自动寻找下一个 CIS 节点的人。
- 能在 Windows 管理员 PowerShell 中执行少量安装和状态命令的用户。

## 不适合哪些情况

- 需要全局 VPN 加速、降低 Raid 延迟或隐藏全部公网流量的人。
- 没有 SoftEther VPN Client 或不接受公共 VPN 中继波动的人。
- 期待项目保证绕过 BSG 账号限制、地区政策或封禁机制的人。本项目不提供这种保证，请遵守游戏和 VPN 服务条款。

## 工作方式

1. `Connect-VpnGateCis.ps1` 优先读取 SoftEther 官方插件已接受并缓存的 `VPNGate.dat` 原生目录，从 `SslPorts` 取得准确的 SoftEther SSL/TCP 端口。
2. 同时查询 VPN Gate 官方 HTTPS/OpenVPN 列表作为新鲜的第二数据源，再按 `RU → UA → 其他配置中的 CIS 候选` 排序并去重。
3. 只接受明确声明了 TCP 端口的候选；原生目录中 `SslPorts` 为空的行，以及 OpenVPN 的 UDP-only 端口，都不会被误当成 SoftEther TCP 端口。
4. 将候选节点写入 SoftEther 账户 `Tarkov-CIS-PlayOnly`，同时验证真实 SoftEther 会话和 VPN DHCP 地址，两者缺一都不会报告成功。
5. 常驻任务同时检查网卡、IPv4 地址和 `vpncmd AccountStatusGet` 会话状态。
6. 当前节点失效时，删除本工具管理的旧路由并尝试下一个；确认属于远端的失败通常会隔离 15 分钟，本机虚拟网卡尚未释放（SoftEther 错误 43）则只等待并有限重试，不会误伤候选节点。如果当前列表中的所有 CIS 节点同时进入隔离，连接器会在约 2 分钟的短暂宽限后轮换最旧的少量失败节点，避免整个候选池长时间“无可用节点”而看起来卡死。
7. 一轮故障转移默认最多运行 180 秒，并优先尝试不同 IP 的独立中继；候选按 CIS 国家轮询，先保证 RU 后的 UA/KZ/BY 等国家各有机会，再补齐同一国家的其他节点；同一中继的其他 SSL 端口只作为后备，不会挤占全部候选名额。
8. 目标域名解析出的地址使用临时 `/32` 路由走 VPN；VPN 默认路由提高 metric，因此日本物理网卡仍是普通流量的默认出口。

成功建立过真实会话的节点会在本机保留 48 小时作为短期备用。该记录位于 Git 忽略的状态文件中，不会作为固定公网 IP 上传到仓库；这可应对 VPN Gate API 暂时漏掉仍可用节点的情况。

原生目录默认只接受 24 小时内的缓存。打开 SoftEther 的“VPN Gate 公共 VPN 中继服务器”并刷新列表，会由官方插件更新该文件；如果文件不存在或过期，工具仍会自动使用 HTTPS 数据源和近期验证成功的节点。原生服务端目录通过 HTTP 分发并附带签名，因此本项目目前不会绕过官方插件直接下载并信任未经校验的原生响应。

Windows 路由按目标 IP 选择，不能按 URL 路径或进程区分流量。如果登录、匹配或其他 HTTPS 服务共享同一个 CDN/IP，它们无法用普通静态路由进一步拆分。独立 Raid 服务器不会因为本项目的已知目标列表而被加入 VPN，但仍应结合本机日志复核实际服务器。

## 环境要求

- Windows 10/11。
- SoftEther VPN Client，且已创建并启用 `VPN - VPN Client` 虚拟网卡。
- PowerShell 5.1 以上；PowerShell 7 推荐但不是强制要求。
- 管理员 PowerShell（安装任务和写入临时路由需要）。
- 可选：EFT 本地日志目录，用于动态发现 lobby/WSN 主机名。

## 图形界面（推荐）

1. 安装并启用 SoftEther VPN Client 的 `VPN - VPN Client` 虚拟网卡。
2. 双击项目根目录的 `Tarkov-CIS-GUI.cmd`。
3. 接受一次 Windows 管理员权限提示。
4. 选择实际的 `EscapeFromTarkov\Logs` 目录。
5. 点击“启动并常驻”。

界面会显示后台任务、VPN 接口、VPN 地址和普通默认出口，并提供以下操作：

- **启动并常驻**：先安装并启动后台任务后立即返回；节点发现、连接和后续故障转移由后台执行，重复点击不会启动第二个连接器。
- **查看详细状态**：显示当前节点、域名解析和选中路由；会话密钥等敏感字段不会显示。
- **停止分流**：停止后台任务、删除临时路由并断开 VPN。
- **卸载后台任务**：移除计划任务、删除临时路由并断开 VPN。

首次启动会根据 `config.example.json` 自动创建本机专用的 `config.json`；该文件已被 Git 忽略。

## 命令行方式

在项目目录中打开“管理员 PowerShell”：

```powershell
Copy-Item .\config.example.json .\config.json
# 编辑 config.json，把 GameLogRoots 改成实际的 EscapeFromTarkov\Logs 路径
notepad .\config.json

Set-ExecutionPolicy -Scope Process Bypass

# 首次手动建立一个可用的 CIS VPN Gate 连接
.\src\Connect-VpnGateCis.ps1 -Action Connect

# 安装并启动后台常驻任务
.\src\Tarkov-CisRouteKeeper.ps1 -Action Install -ConfigPath .\config.json

# 查看任务、VPN、目标域名和选中路由
.\src\Tarkov-CisRouteKeeper.ps1 -Action Status -ConfigPath .\config.json
```

首次连接时，脚本会优先尝试原生目录中的俄罗斯 SoftEther 节点；如果俄罗斯节点没有响应，就会继续尝试乌克兰及其他配置中的 CIS 节点。安装常驻任务后，即使当前 VPN 断开，任务也会自动执行同样的故障转移流程，不需要每次手动加入服务器。

掉线期间后台每 5 秒检查一次状态；一整批候选都失败时，10 秒后会重新读取最新节点目录，而不是继续等待旧列表。若新列表与失败缓存重合，连接器会使用受控的冷却回退批次继续验证，而不是等待完整的隔离周期。单个节点必须同时完成 SoftEther 会话和 IPv4 租约才算连接成功，成功后鉴权路由会立即恢复。可在 `config.json` 中用 `DisconnectedPollSeconds` 和 `FailedCycleRetrySeconds` 调整前两个间隔。

## 日常管理

```powershell
# 手动刷新节点并连接当前最优候选
.\src\Connect-VpnGateCis.ps1 -Action Connect

# 查看原生目录与 HTTPS 数据源合并后的 TCP 候选
.\src\Connect-VpnGateCis.ps1 -Action Candidates

# 查看 SoftEther 账户和网卡状态
.\src\Connect-VpnGateCis.ps1 -Action Status

# 停止后台任务，并删除本工具创建的临时路由
.\src\Tarkov-CisRouteKeeper.ps1 -Action Stop -ConfigPath .\config.json

# 重新启动已经安装的任务
Start-ScheduledTask -TaskName Tarkov-CIS-RouteKeeper

# 卸载任务，并删除本工具创建的临时路由
.\src\Tarkov-CisRouteKeeper.ps1 -Action Uninstall -ConfigPath .\config.json
```

如果你通过 VPN Gate 图形列表手动连接了一个脚本列表中暂未出现的 CIS 节点，可在确认其国家/地区后，将当前真实会话记为短期备用：

```powershell
# 仅在当前会话确实是乌克兰节点时执行；俄罗斯节点把 UA 改为 RU
.\src\Connect-VpnGateCis.ps1 -Action RememberCurrent -RelayCountry UA
```

常驻任务名称为 `Tarkov-CIS-RouteKeeper`。后台日志位于 `src\Tarkov-CisRouteKeeper.log`，会捕获节点尝试、警告和错误，并在 2 MiB 时滚动保留一个历史副本。日志、状态文件和本地配置默认不应提交到 Git；仓库的 `.gitignore` 已排除这些运行时内容。

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

当前核心流程已在 Windows + SoftEther VPN Gate 上验证：CIS 节点动态发现、节点失败后的自动切换、区域/二次授权目标分流、日本普通公网保持直连，以及已知 Raid 目标保持物理网卡出口。BSG 后端、DNS/CDN 和 VPN Gate 列表会变化，提交新问题或贡献时请附上新鲜的本机日志和路由证据，不要直接复制过时的 IP 清单。
