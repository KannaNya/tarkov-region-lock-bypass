# HTTPS health and source-only migration

The canonical runtime is Python. The existing `src/Tarkov-CisRouteKeeper.ps1`
filename forwards future Run/Install actions to `scripts/python-task.ps1`.
Source worktrees require Python 3.11+ and prefer source over potentially stale
frozen binaries. Binary-only distributions still use the packaged executable.
`Install` registers the Task Scheduler action to execute that Python runtime
directly; `scripts/python-task.ps1` remains only the UAC/control/migration
surface. `-Legacy` preserves the old implementation for rollback/owned-state
cleanup.

Editing these files does **not** replace the currently running PowerShell
process. Activation is a separately authorized operation: use the Python task
wrapper's Install action, which stops the old keeper through its cleanup path,
waits for it to finish, then installs/starts the canonical task. No task or
network changes are part of the source repair. A legacy Run with remaining
legacy route ownership refuses to silently abandon that ownership; it requests
controlled migration. Do not manually delete ownership files.

Production Python probes the exact routed IPv4 endpoints with SNI, system CA
and hostname verification enabled. An anonymous HEAD / requests no account
tokens or cookies, follows no redirects, and writes no BSG data. Each probe has
a six-second shared budget, also limited by the cycle failover deadline. Any
HTTP response, including 401/403, establishes response transport only; it does
not prove login, region acceptance, WSS upgrade, or an actual Raid connection.
Empty replies, TLS errors and timeouts are unhealthy. Three consecutive failed
cycles initiate a bounded candidate discovery/failover cycle. Success resets
the counter. A backend outage can also cause failure; this is not proof that a
volunteer relay is bad, so application-only failures do not poison its cooldown.
The probe samples one authorized hostname per resolved IPv4 address.

Transient empty DNS resolution after a successful sync retains last-good /32
routes for the same interface/gateway, reports not-ready, and retries at the
failed-cycle interval. It never claims ready on an empty target set. On a cold
start without last-good targets it reports failed and performs existing owned
cleanup. DNS errors are not treated as evidence to penalize a remote node.
Log discovery augments configured paths using bounded conventional game paths
on fixed local disks, including equivalent configured paths on other drives.
Custom layouts still require GameLogRoots. Only lobby, gw-pvp/season and WSN
hostname families are admitted; literal Raid IPs in logs are excluded.

The Python connector does not modify route metrics: RouteManager snapshots and
persists actual ActiveStore metrics before changing them and restores exact
values during cleanup. The legacy keeper asks its connector to defer metric
changes so the keeper captures the unmodified value. Historical state that
already saved 9000 cannot reveal the earlier true value; it is not guessed.

Routing remains IPv4 /32 only. Shared Cloudflare IPs mean other hostnames using
that exact IP may also take the VPN route; IP routing cannot isolate SNI.
No broad Cloudflare ranges, IPv6 routes, Raid routes, firewall, DNS configuration
or registry changes are introduced. IPv6 bypass and real game/WSS success remain
outside the evidence provided by this transport probe.
