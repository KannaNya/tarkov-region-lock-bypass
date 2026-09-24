"""SoftEther VPN Client adapter for the Python keeper."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import socket
import time
from typing import Any, Callable

from .process_runner import CommandResult, run_command


DEFAULT_VPNCMD = Path(r"C:\Program Files\SoftEther VPN Client\vpncmd_x64.exe")
SESSION_ID_RE = re.compile(r"\bSID-[A-Za-z0-9-]+\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class VpnLease:
    interface_index: int
    interface_alias: str
    ipv4: str
    gateway: str


class SoftEtherError(RuntimeError):
    def __init__(self, message: str, result: CommandResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class LocalResourceBusyError(SoftEtherError):
    """SoftEther exit code 43: local session resources are still busy."""


Runner = Callable[..., CommandResult]


def _powershell_quote(value: str) -> str:
    """Quote a literal embedded in a PowerShell single-quoted string."""

    return "'" + value.replace("'", "''") + "'"


class SoftEtherClient:
    """Configure one SoftEther account and verify its real Windows lease.

    ``AccountConnect`` returning zero is intentionally not considered success.
    A usable connection must expose both a locale-independent SoftEther session
    ID and an up adapter with a non-APIPA IPv4 address and gateway.
    """

    def __init__(
        self,
        *,
        vpncmd_path: str | Path = DEFAULT_VPNCMD,
        account_name: str = "Tarkov-CIS-PlayOnly",
        interface_alias: str = "VPN - VPN Client",
        nic_name: str = "VPN",
        powershell: str = "powershell.exe",
        runner: Runner = run_command,
        command_timeout: float = 15.0,
        operation_guard: Callable[[], None] | None = None,
    ) -> None:
        self.vpncmd_path = Path(vpncmd_path)
        self.account_name = account_name
        self.interface_alias = interface_alias
        self.nic_name = nic_name
        self.powershell = powershell
        self._run = runner
        self.command_timeout = command_timeout
        self.operation_guard = operation_guard

    def _check_operation(self) -> None:
        if self.operation_guard is not None:
            self.operation_guard()

    def _vpncmd(self, *arguments: str, timeout: float | None = None) -> CommandResult:
        self._check_operation()
        result = self._run(
            [
                str(self.vpncmd_path),
                "/CLIENT",
                "localhost",
                "/CMD",
                *arguments,
            ],
            timeout=timeout or self.command_timeout,
            check=False,
        )
        if not result.ok:
            operation = arguments[0] if arguments else "vpncmd"
            detail = result.stderr.strip() or result.stdout.strip()
            suffix = f": {detail}" if detail else ""
            error_type = LocalResourceBusyError if result.exit_code == 43 else SoftEtherError
            raise error_type(f"{operation} failed with exit code {result.exit_code}{suffix}", result)
        return result

    def account_status(self, *, timeout: float | None = None) -> CommandResult:
        # /CSV improves stability of the machine-readable fields, while the
        # SID check remains independent of the installed UI language.
        self._check_operation()
        return self._run(
            [
                str(self.vpncmd_path),
                "/CSV",
                "/CLIENT",
                "localhost",
                "/CMD",
                "AccountStatusGet",
                self.account_name,
            ],
            timeout=timeout or self.command_timeout,
            check=False,
        )

    def has_established_session(self, *, timeout: float | None = None) -> bool:
        result = self.account_status(timeout=timeout)
        if not result.ok:
            return False
        return SESSION_ID_RE.search(result.stdout + "\n" + result.stderr) is not None

    def get_lease(self, *, timeout: float | None = None) -> VpnLease | None:
        self._check_operation()
        alias = _powershell_quote(self.interface_alias)
        script = (
            "$ErrorActionPreference='Stop';"
            f"$a=Get-NetAdapter -InterfaceAlias {alias} -ErrorAction Stop;"
            "if($a.Status -ne 'Up'){exit 3};"
            "$c=Get-NetIPConfiguration -InterfaceIndex $a.ifIndex -ErrorAction Stop;"
            "$ip=@($c.IPv4Address|Where-Object {"
            "$_.IPAddress -and $_.IPAddress -notlike '169.254.*' -and "
            "$_.IPAddress -ne '0.0.0.0'}|Select-Object -ExpandProperty IPAddress -First 1);"
            "$gw=@($c.IPv4DefaultGateway|Where-Object NextHop|"
            "Select-Object -ExpandProperty NextHop -First 1);"
            "if(-not $ip -or -not $gw){exit 4};"
            "[pscustomobject]@{InterfaceIndex=[int]$a.ifIndex;"
            "InterfaceAlias=[string]$a.InterfaceAlias;IPv4=[string]$ip;"
            "Gateway=[string]$gw}|ConvertTo-Json -Compress"
        )
        result = self._run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=timeout or self.command_timeout,
            check=False,
        )
        if not result.ok or not result.stdout.strip():
            return None
        try:
            raw = json.loads(result.stdout.strip())
            if isinstance(raw, list):
                raw = raw[0]
            ipv4 = ipaddress.IPv4Address(str(raw["IPv4"]))
            gateway = ipaddress.IPv4Address(str(raw["Gateway"]))
            if ipv4.is_link_local or ipv4.is_unspecified or gateway.is_unspecified:
                return None
            return VpnLease(
                interface_index=int(raw["InterfaceIndex"]),
                interface_alias=str(raw["InterfaceAlias"]),
                ipv4=str(ipv4),
                gateway=str(gateway),
            )
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def verified_connection(self, *, timeout: float | None = None) -> VpnLease | None:
        if not self.has_established_session(timeout=timeout):
            return None
        return self.get_lease(timeout=timeout)

    def probe_tcp(self, relay: Any, *, timeout: float = 1.5) -> bool:
        self._check_operation()
        host = str(getattr(relay, "ip", "") or getattr(relay, "host_name", ""))
        port = int(getattr(relay, "port", 0))
        if not host or not 1 <= port <= 65535:
            return False
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _account_exists(self, *, timeout: float | None = None) -> bool:
        result = self._vpncmd("AccountList", timeout=timeout)
        return re.search(
            rf"(?im)(?:^|[|\s]){re.escape(self.account_name)}(?:$|[|\s])",
            result.stdout,
        ) is not None

    def ensure_account(self, relay: Any, *, deadline: float | None = None) -> None:
        host = str(getattr(relay, "ip", "") or getattr(relay, "host_name", ""))
        port = int(getattr(relay, "port", 0))
        if not host or not 1 <= port <= 65535:
            raise ValueError("relay must provide a host/IP and TCP port")
        server = f"/SERVER:{host}:{port}"
        def remaining() -> float:
            if deadline is None:
                return self.command_timeout
            value = deadline - time.monotonic()
            if value <= 0:
                raise SoftEtherError("failover deadline expired during account configuration")
            return min(self.command_timeout, value)

        if self._account_exists(timeout=remaining()):
            self._vpncmd(
                "AccountSet", self.account_name, server, "/HUB:VPNGATE", timeout=remaining()
            )
        else:
            self._vpncmd(
                "AccountCreate",
                self.account_name,
                server,
                "/HUB:VPNGATE",
                "/USERNAME:vpn",
                f"/NICNAME:{self.nic_name}",
                timeout=remaining(),
            )
            self._vpncmd(
                "AccountPasswordSet",
                self.account_name,
                "/PASSWORD:vpn",
                "/TYPE:standard",
                timeout=remaining(),
            )
        # The Python keeper owns relay rotation; disable SoftEther's unbounded
        # internal endpoint retries.
        self._vpncmd(
            "AccountRetrySet",
            self.account_name,
            "/NUM:0",
            "/INTERVAL:5",
            timeout=remaining(),
        )
        self._vpncmd("AccountStatusHide", self.account_name, timeout=remaining())

    def disconnect(self, *, timeout: float = 15.0, poll_interval: float = 0.25) -> bool:
        self._check_operation()
        deadline = time.monotonic() + max(timeout, 0.1)
        result = self._run(
            [
                str(self.vpncmd_path),
                "/CLIENT",
                "localhost",
                "/CMD",
                "AccountDisconnect",
                self.account_name,
            ],
            timeout=min(self.command_timeout, max(deadline - time.monotonic(), 0.1)),
            check=False,
        )
        while time.monotonic() <= deadline:
            remaining = max(0.1, deadline - time.monotonic())
            if not self.has_established_session(
                timeout=min(self.command_timeout, remaining)
            ) and self.get_lease(timeout=min(self.command_timeout, remaining)) is None:
                return True
            time.sleep(max(0.01, poll_interval))
        if not result.ok:
            error_type = (
                LocalResourceBusyError if result.exit_code == 43 else SoftEtherError
            )
            raise error_type(
                f"AccountDisconnect failed with exit code {result.exit_code}", result
            )
        return False

    def connect(
        self,
        relay: Any,
        *,
        timeout: float = 18.0,
        poll_interval: float = 0.5,
    ) -> VpnLease:
        # Failure to observe a prior session is acceptable; a stale adapter is
        # not.  The bounded disconnect prevents reconfiguration while the local
        # SoftEther resources are still busy.
        deadline = time.monotonic() + max(timeout, 0.1)

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise SoftEtherError("failover deadline expired")
            return value

        if not self.disconnect(
            timeout=min(remaining(), 15.0), poll_interval=poll_interval
        ):
            raise SoftEtherError("previous SoftEther session did not release")
        self.ensure_account(relay, deadline=deadline)
        self._vpncmd(
            "AccountConnect",
            self.account_name,
            timeout=min(self.command_timeout, remaining()),
        )

        while time.monotonic() <= deadline:
            lease = self.verified_connection(
                timeout=min(self.command_timeout, max(0.1, remaining()))
            )
            if lease is not None:
                return lease
            time.sleep(max(0.01, poll_interval))

        try:
            self.disconnect(timeout=min(timeout, 5.0), poll_interval=poll_interval)
        except SoftEtherError:
            pass
        raise SoftEtherError(
            "SoftEther handshake did not produce both an established session and a non-APIPA IPv4 lease"
        )
