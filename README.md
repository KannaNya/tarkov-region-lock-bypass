# Tarkov Region Lock Bypass (Windows / PowerShell)

建议 GitHub 仓库名：`tarkov-region-lock-bypass`

这是一个不依赖 Python 的 Windows PowerShell 工具，用 SoftEther VPN Gate 只承载已验证的 Tarkov 区域验证和游戏后端域名；普通日本公网和独立 Raid 服务器继续走日本物理网卡。

This project intentionally uses Windows routing and DNS observations from the local machine. It does not ship a fixed “Tarkov IP list”. The keeper resolves configured domains repeatedly and can discover lobby/WSN hostnames from recent EFT backend logs.

## What it does

- Works with an already-connected SoftEther VPN Gate virtual adapter.
- Maintains only current IPv4 `/32` routes for configured Tarkov backend hosts.
- Keeps the VPN default route at a high metric so ordinary traffic stays on the physical/Japan adapter.
- Removes its own routes when the VPN disconnects.
- Detects a changed VPN interface/gateway and removes the previous generation of routes before adding new ones.
- Installs as a hidden per-user scheduled task; no Python runtime is required.

## Important limitation

Windows routes select by destination IP, not URL path or process. If Tarkov authentication, lobby, matching HTTPS, or other APIs share an IP/CDN address, they cannot be split from one another with ordinary static routes. Independent Raid server IPs/UDP are not added by this project, but you should verify the current session from local logs rather than trust an old list.

This project does not bypass account restrictions or guarantee that a VPN exit is accepted by Battlestate Games. Use an account and VPN service in accordance with their terms.

## Requirements

- Windows 10/11 with elevated PowerShell for installation.
- SoftEther VPN Client and a connected VPN Gate virtual adapter.
- PowerShell 5.1+; PowerShell 7 is recommended but not required.
- Optional: local EFT log directory in `config.json` for dynamic WSN discovery.

## Quick start

```powershell
Copy-Item .\config.example.json .\config.json
# Edit config.json: set GameLogRoots to your actual EscapeFromTarkov\Logs directory.

Set-ExecutionPolicy -Scope Process Bypass
.\src\Tarkov-CisRouteKeeper.ps1 -Action Install -ConfigPath .\config.json
.\src\Tarkov-CisRouteKeeper.ps1 -Action Status -ConfigPath .\config.json
```

Connect SoftEther/VPN Gate before launching the game. The task keeps the selected backend routes while the VPN is connected. The ordinary default route remains on the Japanese adapter.

## Management

```powershell
# Stop the worker and remove only routes owned by it
.\src\Tarkov-CisRouteKeeper.ps1 -Action Stop -ConfigPath .\config.json

# Start the already-installed task again
Start-ScheduledTask -TaskName Tarkov-CIS-RouteKeeper

# Remove the scheduled task and owned routes
.\src\Tarkov-CisRouteKeeper.ps1 -Action Uninstall -ConfigPath .\config.json
```

Use `Status` to inspect observed hosts, selected interfaces, default route metrics, and configured Raid safety targets.

## Safety and privacy

- Never commit `config.json`, account credentials, VPN session keys, launcher logs, or route state files.
- The project does not modify firewall rules, DNS servers, registry values, or permanent routes.
- The task uses Windows `ActiveStore` host routes and can be removed with `Stop` or `Uninstall`.
- Review the current route table before testing. If the VPN becomes the selected default route, stop the task and restore the physical default route.

## Project status

The core flow has been tested on Windows with SoftEther VPN Gate: launcher region authorization, second profile authorization, Japanese ordinary egress, and known Raid target selection. DNS/CDN layouts can change; contributions should include fresh local evidence and avoid publishing stale IP ranges.
