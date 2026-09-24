# Python 重构架构

Python 版本采用渐进迁移：现有 PowerShell 实现继续作为兼容和回滚入口，新的 Python 核心负责长期运行、节点选择、显式状态转换、SoftEther 会话验证与临时路由生命周期。

## 模块边界

```text
tarkov_cis.cli / tarkov_cis.gui
                |
          tarkov_cis.keeper
          /       |       \
   catalog    softether   routing
      |           |          |
relay_selector  process_runner  Windows ActiveStore
      |
 state_machine + models + config + game_phase
```

- `models.py`：不可变数据模型，不执行系统命令。
- `config.py`：读取并验证与旧版兼容的 `config.json`。
- `catalog.py`：解析 VPN Gate 数据源，区分 SoftEther TCP 端口、UDP NAT-T 端口和不能混用的 OpenVPN UDP 端口。
- `relay_selector.py`：国家轮询、端点去重、失败冷却和受控回退。
- `state_machine.py`：限制连接阶段的合法转换，避免用散乱布尔变量表达状态。
- `process_runner.py`：为所有外部命令统一提供超时、退出码和输出捕获。
- `softether.py`：唯一允许调用 `vpncmd` 的模块；UDP NAT-T 使用 SoftEther 自身导出/导入连接设置的 `PortUDP` 字段；SID 与非 APIPA IPv4 租约同时存在才算成功。
- `routing.py`：只管理本项目记录的目标 `/32` ActiveStore 路由，并把 VPN 默认路由 metric 提高到 9000。
- `eft_logs.py`：只发现登录、lobby、WSN 和 gw-pvp 主机名；不把 Raid IP 自动加入 VPN。
- `game_phase.py`：从 EFT 本地日志读取登录、选角色、匹配、Raid、PostRaid 标记；仅在游戏进程仍存在时启用匹配/Raid 保护锁。
- `keeper.py`：由 `ConnectionStateMachine` 驱动连接、验证、路由同步、健康检查和节点切换。
- `gui.py`：最小 Tkinter 控制面，只调用后台任务控制回调；关闭窗口不会停止 Keeper。
- `cli.py`：配置、只读状态、KnownGood/失败状态与 CLI 编排。
- `scripts/python-task.ps1`：只负责 UAC、迁移和计划任务控制；它注册的常驻任务动作直接执行 Python `run`，自身不作为长期 Keeper 进程。

## 安全不变量

1. 不修改防火墙、DNS、注册表或永久路由。
2. 不把 `RaidTargets` 自动加入 VPN；兼容字段目前保留但不参与路由。
3. 默认路由必须仍由非 VPN 接口选中。
4. 只有 SID 与有效 VPN IPv4/DHCP 网关同时存在，连接状态才是 `READY`。
5. SoftEther 本地资源忙（错误 43）不应冷却远端中继。
6. 日志、状态文件、PID 和本机 `config.json` 不进入 Git。
7. 所有外部命令必须有超时，失败必须携带命令名、退出码与可审计信息。
8. PID、操作 token、进程创建时间和随机 generation 必须共同匹配；旧世代状态只标记为 stale，不得伪装成当前 `READY`。
9. 停止失败时保留所有权记录并保持单实例锁，直到工作线程结束；不得为了让界面显示“已停止”而与仍在执行的路由操作竞态。
10. 默认模式在登录、选角、大厅和匹配时保持鉴权路由与 SoftEther 会话；只有带时间戳的 `GameStarted:` 证明进入 Raid 后才撤销自有路由并断开 SoftEther，同一游戏会话内不再重连。`MainMenuShowOperation` 常出现在错误堆栈中，不作为断开依据；旧的匹配/Raid 保持会话保护只在 `DisconnectAtRaid=false` 时启用。

## 发布方式

开发环境可执行 `py -3 Tarkov-CIS-Python.py gui`。面向没有 Python 的用户，由 `scripts/build-python.ps1` 使用 PyInstaller 生成 `dist/TarkovCIS-Windows.zip`；最终用户仍只需要安装 SoftEther VPN Client。GitHub Actions 同时保留 Python 与旧 PowerShell 的 Windows 回归测试。
