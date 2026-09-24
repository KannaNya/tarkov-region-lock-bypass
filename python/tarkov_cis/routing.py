"""Ephemeral Windows /32 route ownership for authorization endpoints only."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import time
from typing import Callable, Iterable

from .process_runner import CommandResult, run_command
from .game_phase import PlayProtectionActivated
from .softether import VpnLease, _powershell_quote


class RouteError(RuntimeError):
    def __init__(self, message: str, result: CommandResult | None = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True, slots=True)
class ManagedRoute:
    ip: str
    interface_index: int
    gateway: str

    @property
    def prefix(self) -> str:
        return f"{self.ip}/32"


@dataclass(frozen=True, slots=True)
class OriginalDefaultRoute:
    interface_index: int
    next_hop: str
    route_metric: int


Runner = Callable[..., CommandResult]


def _target_ipv4(value: str) -> str:
    address = ipaddress.ip_address(value)
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValueError("only IPv4 authorization targets are supported")
    if address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast:
        raise ValueError(f"unsafe authorization target: {address}")
    return str(address)


class RouteManager:
    """Own only routes created by this process in ``ActiveStore``.

    Default-route handling is limited to temporarily raising the VPN route's
    metric in ``ActiveStore`` and restoring the exact previous value.  The
    physical default, interface metric, firewall, DNS and registry are never
    changed.  Pre-existing matching /32 routes are left untouched and are not
    claimed as owned.
    """

    def __init__(
        self,
        *,
        powershell: str = "powershell.exe",
        runner: Runner = run_command,
        command_timeout: float = 10.0,
        route_metric: int = 1,
        state_path: str | Path | None = None,
        operation_guard: Callable[[], None] | None = None,
    ) -> None:
        if route_metric < 1:
            raise ValueError("route_metric must be positive")
        self.powershell = powershell
        self._run = runner
        self.command_timeout = command_timeout
        self.operation_guard = operation_guard
        self.route_metric = route_metric
        self._managed: set[ManagedRoute] = set()
        local_app_data = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
        self.state_path = Path(state_path) if state_path else (
            local_app_data / "TarkovCIS" / "routes.json"
        )
        self._original_defaults: set[OriginalDefaultRoute] = set()
        self._load_state()

    @property
    def managed(self) -> tuple[ManagedRoute, ...]:
        return tuple(sorted(self._managed, key=lambda item: (item.ip, item.interface_index)))

    def _load_state(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
            if raw.get("version") != 1:
                return
            self._managed = {
                ManagedRoute(
                    ip=_target_ipv4(item["ip"]),
                    interface_index=int(item["interface_index"]),
                    gateway=str(ipaddress.IPv4Address(item["gateway"])),
                )
                for item in raw.get("managed_routes", ())
            }
            self._original_defaults = {
                OriginalDefaultRoute(
                    interface_index=int(item["interface_index"]),
                    next_hop=str(ipaddress.IPv4Address(item["next_hop"])),
                    route_metric=int(item["route_metric"]),
                )
                for item in raw.get("original_defaults", ())
            }
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            # An invalid state file is never a license to remove unknown routes.
            self._managed.clear()
            self._original_defaults.clear()

    def _save_state(self) -> None:
        if not self._managed and not self._original_defaults:
            try:
                self.state_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        payload = {
            "version": 1,
            "managed_routes": [
                {
                    "ip": route.ip,
                    "interface_index": route.interface_index,
                    "gateway": route.gateway,
                }
                for route in self.managed
            ],
            "original_defaults": [
                {
                    "interface_index": route.interface_index,
                    "next_hop": route.next_hop,
                    "route_metric": route.route_metric,
                }
                for route in sorted(
                    self._original_defaults,
                    key=lambda item: (item.interface_index, item.next_hop),
                )
            ],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.state_path)

    def _bounded_timeout(self, deadline: float | None) -> float:
        if deadline is None:
            return self.command_timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RouteError("authorization route synchronization deadline expired")
        return min(self.command_timeout, max(0.1, remaining))

    def _powershell(self, script: str, *, deadline: float | None = None) -> CommandResult:
        if self.operation_guard is not None:
            self.operation_guard()
        return self._run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=self._bounded_timeout(deadline),
            check=False,
        )

    def _exists(self, route: ManagedRoute, *, deadline: float | None = None) -> bool:
        prefix = _powershell_quote(route.prefix)
        gateway = _powershell_quote(route.gateway)
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$r=@(Get-NetRoute -PolicyStore ActiveStore -AddressFamily IPv4 "
            f"-DestinationPrefix {prefix} -InterfaceIndex {route.interface_index}|"
            f"Where-Object NextHop -eq {gateway});"
            "if($r.Count -gt 0){exit 0}else{exit 1}"
        )
        return self._powershell(script, deadline=deadline).ok

    def add_target(
        self, ip: str, lease: VpnLease, *, deadline: float | None = None
    ) -> bool:
        normalized_ip = _target_ipv4(ip)
        gateway = str(ipaddress.IPv4Address(lease.gateway))
        route = ManagedRoute(normalized_ip, int(lease.interface_index), gateway)
        if self._exists(route, deadline=deadline):
            return False

        prefix = _powershell_quote(route.prefix)
        next_hop = _powershell_quote(route.gateway)
        script = (
            "$ErrorActionPreference='Stop';"
            f"New-NetRoute -PolicyStore ActiveStore -AddressFamily IPv4 "
            f"-DestinationPrefix {prefix} -InterfaceIndex {route.interface_index} "
            f"-NextHop {next_hop} -RouteMetric {self.route_metric} -ErrorAction Stop|Out-Null"
        )
        result = self._powershell(script, deadline=deadline)
        if not result.ok:
            raise RouteError(
                f"failed to add temporary route {route.prefix} on interface {route.interface_index}",
                result,
            )
        self._managed.add(route)
        self._save_state()
        return True

    def remove_target(
        self, route: ManagedRoute, *, deadline: float | None = None
    ) -> bool:
        # Only an exact route identity previously recorded by this instance is
        # eligible for deletion.
        if route not in self._managed:
            return False
        prefix = _powershell_quote(route.prefix)
        gateway = _powershell_quote(route.gateway)
        script = (
            "$ErrorActionPreference='Stop';"
            f"$r=@(Get-NetRoute -PolicyStore ActiveStore -AddressFamily IPv4 "
            f"-DestinationPrefix {prefix} -InterfaceIndex {route.interface_index} "
            f"-ErrorAction SilentlyContinue|Where-Object NextHop -eq {gateway});"
            # A disconnected SoftEther adapter removes its routes before the
            # keeper gets a chance to clean up its ownership file.  Treat an
            # exact route that is already absent as successful; if the same
            # interface index is later reused, the exact gateway/prefix filter
            # still prevents removing an unrelated route.
            "if($r.Count -eq 0){exit 0};"
            # SoftEther can remove the route between the exact read above and
            # Remove-NetRoute.  Re-read the same prefix/interface/gateway
            # after a removal error; an empty result is already the desired
            # state and must not block failover or shutdown.
            "try{$r|Remove-NetRoute -PolicyStore ActiveStore -Confirm:$false -ErrorAction Stop}"
            "catch{$left=@(Get-NetRoute -PolicyStore ActiveStore -AddressFamily IPv4 "
            f"-DestinationPrefix {prefix} -InterfaceIndex {route.interface_index} "
            f"-ErrorAction SilentlyContinue|Where-Object NextHop -eq {gateway});"
            "if($left.Count -eq 0){exit 0};throw $_}"
        )
        result = self._powershell(script, deadline=deadline)
        if not result.ok:
            raise RouteError(f"failed to remove temporary route {route.prefix}", result)
        self._managed.discard(route)
        self._save_state()
        return True

    def _read_vpn_defaults(
        self, lease: VpnLease, *, deadline: float | None = None
    ) -> tuple[OriginalDefaultRoute, ...]:
        script = (
            "$ErrorActionPreference='Stop';"
            f"@(Get-NetRoute -PolicyStore ActiveStore -AddressFamily IPv4 "
            f"-DestinationPrefix '0.0.0.0/0' -InterfaceIndex {int(lease.interface_index)} "
            "-ErrorAction SilentlyContinue|Select-Object InterfaceIndex,NextHop,RouteMetric)|"
            "ConvertTo-Json -Compress"
        )
        result = self._powershell(script, deadline=deadline)
        if not result.ok:
            raise RouteError("failed to inspect VPN default routes", result)
        if not result.stdout.strip():
            return ()
        try:
            raw = json.loads(result.stdout)
            entries = raw if isinstance(raw, list) else [raw]
            return tuple(
                OriginalDefaultRoute(
                    interface_index=int(item["InterfaceIndex"]),
                    next_hop=str(ipaddress.IPv4Address(item["NextHop"])),
                    route_metric=int(item["RouteMetric"]),
                )
                for item in entries
                if item and item.get("NextHop")
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RouteError("PowerShell returned invalid VPN default-route data", result) from exc

    def _set_default_metric(
        self,
        route: OriginalDefaultRoute,
        metric: int,
        *,
        deadline: float | None = None,
    ) -> None:
        next_hop = _powershell_quote(route.next_hop)
        script = (
            "$ErrorActionPreference='Stop';"
            f"$r=@(Get-NetRoute -PolicyStore ActiveStore -AddressFamily IPv4 "
            f"-DestinationPrefix '0.0.0.0/0' -InterfaceIndex {route.interface_index} "
            f"-ErrorAction SilentlyContinue|Where-Object NextHop -eq {next_hop});"
            # The virtual adapter may already be gone.  There is then no
            # default route to restore, and retaining the stale ownership entry
            # would block the next discovery cycle forever.
            "if($r.Count -eq 0){exit 0};"
            f"$r|Set-NetRoute -PolicyStore ActiveStore -RouteMetric {int(metric)} "
            "-ErrorAction Stop"
        )
        result = self._powershell(script, deadline=deadline)
        if not result.ok:
            raise RouteError(
                f"failed to set ActiveStore default-route metric on interface {route.interface_index}",
                result,
            )

    def _best_default_interface(self, *, deadline: float | None = None) -> int:
        script = (
            "$ErrorActionPreference='Stop';"
            "$ranked=@(Get-NetRoute -PolicyStore ActiveStore -AddressFamily IPv4 "
            "-DestinationPrefix '0.0.0.0/0' -ErrorAction Stop|"
            "Where-Object State -eq 'Alive'|ForEach-Object {"
            "$r=$_;$i=Get-NetIPInterface -PolicyStore ActiveStore -AddressFamily IPv4 "
            "-InterfaceIndex $r.InterfaceIndex -ErrorAction Stop|Select-Object -First 1;"
            "[pscustomobject]@{InterfaceIndex=[int]$r.InterfaceIndex;"
            "NextHop=[string]$r.NextHop;EffectiveMetric=[int]$r.RouteMetric+[int]$i.InterfaceMetric}}|"
            "Sort-Object EffectiveMetric|Select-Object -First 1);"
            "if(-not $ranked){exit 5};$ranked|ConvertTo-Json -Compress"
        )
        result = self._powershell(script, deadline=deadline)
        if not result.ok or not result.stdout.strip():
            raise RouteError("no live IPv4 default route remains", result)
        try:
            return int(json.loads(result.stdout)["InterfaceIndex"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RouteError("PowerShell returned invalid default-route ranking data", result) from exc

    def protect_physical_default(
        self, lease: VpnLease, *, deadline: float | None = None
    ) -> None:
        """Raise only VPN ActiveStore defaults, then verify physical still wins."""

        vpn_defaults = self._read_vpn_defaults(lease, deadline=deadline)
        original_identities = {
            (route.interface_index, route.next_hop) for route in self._original_defaults
        }
        new_originals = [
            route
            for route in vpn_defaults
            if (route.interface_index, route.next_hop) not in original_identities
        ]
        if new_originals:
            self._original_defaults.update(new_originals)
            # Persist originals before changing Windows so a separate stop
            # process can restore them after an unexpected exit.
            self._save_state()
        for route in vpn_defaults:
            self._set_default_metric(route, 9000, deadline=deadline)
        if self._best_default_interface(deadline=deadline) == int(lease.interface_index):
            raise RouteError("VPN is still the selected IPv4 default route")

    def sync(
        self,
        target_ips: Iterable[str],
        lease: VpnLease,
        *,
        deadline: float | None = None,
    ) -> tuple[ManagedRoute, ...]:
        desired = {_target_ipv4(ip) for ip in target_ips}
        identity = (int(lease.interface_index), str(ipaddress.IPv4Address(lease.gateway)))

        if not desired:
            raise RouteError("empty DNS target set; existing routes retained; use cleanup explicitly")

        stale_identity = any(
            (route.interface_index, route.gateway) != identity for route in self._managed
        ) or any(
            route.interface_index != int(lease.interface_index)
            for route in self._original_defaults
        )
        if stale_identity:
            self.cleanup(deadline=deadline)

        self.protect_physical_default(lease, deadline=deadline)

        for route in tuple(self._managed):
            if route.ip not in desired or (route.interface_index, route.gateway) != identity:
                self.remove_target(route, deadline=deadline)
        for ip in sorted(desired):
            self.add_target(ip, lease, deadline=deadline)
        return self.managed

    def cleanup(self, *, deadline: float | None = None) -> None:
        errors: list[Exception] = []
        for route in tuple(self._managed):
            try:
                self.remove_target(route, deadline=deadline)
            except PlayProtectionActivated:
                raise
            except Exception as exc:  # retain all cleanup attempts
                errors.append(exc)
        for route in tuple(self._original_defaults):
            try:
                self._set_default_metric(route, route.route_metric, deadline=deadline)
                self._original_defaults.discard(route)
                self._save_state()
            except PlayProtectionActivated:
                raise
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RouteError(f"failed to restore {len(errors)} managed network change(s)") from errors[0]
