# Tarkov CIS VPN Gate 项目交接

更新时间：2026-09-24（Asia/Tokyo）

## 当前仓库状态

- GitHub：<https://github.com/KannaNya/tarkov-region-lock-bypass>
- 默认分支：`master`
- Raid 保护功能提交：`03f381eaabb76452e28f521db5718fa57bb434bd`
- 交接文档最新提交：以远端 `master` 当前 HEAD 为准（本次文档提交之后会继续更新）。
- 本地工作目录：`C:\Users\Violet\Documents\Codex\2026-08-08\escape-from-tarkov-cis-vpn-gate`
- 当前本地分支：`codex/python-refactor`；其 HEAD 与远端 `master` 相同。
- 运行日志、`config.json`、构建产物和本机状态均已被忽略，没有上传。

## 用户目标

在日本使用 Tarkov CIS 区账号时，只让登录、地区鉴权和必要的 Launcher/API 目标走 CIS VPN Gate；日本默认出口继续承担更新、匹配和 Raid 游戏流量。VPN Gate 节点失效时可以在不玩游戏时切换，但不能在匹配或 Raid 中断开当前 VPN。

## 本轮已完成的关键功能

1. `python/tarkov_cis/game_phase.py` 从真实 EFT 日志识别登录、选角色、匹配、Raid、PostRaid 和菜单阶段。
2. 保护起点是 `TRACE-NetworkGameMatching`/`MatchingCompleted`；`UserConfirmed`、`TRACE-NetworkGameCreate`、`GameStarted` 继续保持保护。
3. 匹配/Raid 期间禁止：会话探测、HTTPS 健康探测、DNS 鉴权刷新、清理临时路由、SoftEther disconnect、`AccountSet`/`AccountConnect` 和节点切换。
4. 保护期间保留现有 SoftEther 会话和鉴权 `/32` 路由；Raid 服务器 IP 只作日志证据，不能自动加入 VPN 路由。
5. `UserMatchOver`、`PostRaid`、菜单标记、游戏退出或新日志会话出现后，恢复正常维护。
6. Astra 修复了核心竞态：阶段检查不再只发生在一个 `run_cycle()` 开头，而是在 SoftEther 和 Windows 路由的每个后续动作前复查。
7. GUI/状态 JSON 增加 `game_phase`、`game_evidence` 和 `play_protected`；保护期间状态界面不会为了显示 VPN 状态而主动探测并触发断线。
8. `config.json` 当前启用：`PauseDuringRaid=true`。示例配置和 README 已同步。

## 已验证结果

- Python 全套测试：101/101 通过。
- 游戏保护专项测试：33/33 通过。
- Python `compileall` 通过。
- PowerShell 语法检查通过。
- 路由 Keeper 检查通过。
- `git diff --check` 通过。
- 本机真实日志目录 `C:\GAME\Tarkov\Logs` 中已确认存在 `ShowCharacterSelectionScreen`、`TRACE-NetworkGameMatching`、`TRACE-NetworkGameCreate`、`GameStarted`、`userConfirmed`、`userMatchOver` 和 `PostRaid` 等标记。
- 后台任务已在确认 `EscapeFromTarkov.exe` 未运行后重载为 Python 实现；重载后的任务 PID 曾为 5128，状态显示 `game_phase=menu`、`play_protected=false`。

## 当前限制和未完成验证

- 重载时 VPN Gate 实时节点不可用，后台当时处于候选节点连接失败/重试状态，没有有效 VPN IPv4；这属于节点可用性，不代表 Raid 保护代码失败。
- 没有在真实 Raid 中人为制造节点切换来做破坏性测试，以免掉线或破坏用户会话；竞态由本地日志 fixture、SoftEther 命令序列和路由命令测试覆盖。
- 日志保护不能撤回在阶段标记出现之前已经发出的系统命令；它能保证下一条动作不会继续执行。因此匹配标记刚出现的极短窗口仍需以实际日志时序为准。
- Windows 按目标 IP 路由，不能按进程名区分流量；当前设计依赖鉴权域名解析出的临时 `/32`，不会把 Raid IP 自动加入 VPN。

## 接手后的安全操作

1. 先查看 `git status`、`git log -3 --oneline` 和远端 `master`，不要 reset 或覆盖现有提交。
2. 运行测试：

   ```powershell
   py -3 -m unittest discover -s tests_py -p "test_*.py" -q
   py -3 -m compileall -q python
   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File tests/syntax-check.ps1
   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File tests/route-keeper-check.ps1
   ```

3. 查看后台状态：

   ```powershell
   py -3 Tarkov-CIS-Python.py status --config config.json
   ```

4. 如果 `EscapeFromTarkov.exe` 正在运行，不要重启或停止 `Tarkov-CIS-RouteKeeper`，也不要手动清理 VPN 路由。只有用户回到菜单或退出游戏后才允许重载后台任务。
5. 不要把 `C:\GAME\Tarkov\Logs`、`config.json`、`%LOCALAPPDATA%\TarkovCIS`、SoftEther 凭据或运行日志提交到 GitHub。
6. 如果需要继续优化，优先先复现并记录 `game_phase`、`game_evidence`、`play_protected` 和后台 PID；不要先修改 DNS、防火墙或永久路由。

## 期望的用户验收流程

1. 等 Keeper 成功建立 CIS 会话。
2. 启动 Launcher 登录并进入选角色：此时允许正常鉴权检查。
3. 点击 Play：状态应从匹配阶段变为 `play_protected=true`，节点不应被切换。
4. 在 Raid 中观察不会出现 SoftEther 断开、路由清理或候选节点轮换。
5. 回到菜单并出现 `UserMatchOver`/`PostRaid` 后，保护解除，后台恢复正常维护。
