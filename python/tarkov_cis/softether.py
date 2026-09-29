"""SoftEther VPN Client control through vpncmd, lease checks through iphlpapi."""

from __future__ import annotations

import ipaddress
from pathlib import Path
import re
import socket
import tempfile
import time
from typing import Callable

from .models import Relay, VpnLease
from .process import CommandResult, Runner, run

SESSION_ID_RE = re.compile(r"\bSID-[A-Za-z0-9-]+\b", re.IGNORECASE)
LOCAL_BUSY_EXIT_CODE = 43
Guard = Callable[[], None]


def _no_guard() -> None:
    return None


class SoftEtherError(RuntimeError):
    def __init__(self, message: str, result: CommandResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class LocalBusyError(SoftEtherError):
    """vpncmd exit code 43: local client resources are busy, not a relay fault."""


class SoftEther:
    """One dedicated SoftEther account, re-pointed at each relay we try.

    AccountConnect returning 0 is not success: a relay is only usable once
    the client reports a session ID *and* the virtual adapter has a real IPv4
    address with a gateway.
    """

    def __init__(
        self,
        *,
        vpncmd_path: str,
        account_name: str,
        interface_alias: str,
        nic_name: str,
        runner: Runner = run,
        net=None,
        command_timeout: float = 15.0,
    ) -> None:
        self.vpncmd_path = vpncmd_path
        self.account_name = account_name
        self.interface_alias = interface_alias
        self.nic_name = nic_name
        self._run = runner
        self._net = net
        self.command_timeout = command_timeout

    @property
    def net(self):
        if self._net is None:
            from .winapi import iphlpapi

            self._net = iphlpapi
        return self._net

    # --- read-only checks ---------------------------------------------------

    def adapter_lease(self) -> VpnLease | None:
        """The adapter's non-APIPA IPv4 and its DHCP gateway, if both exist."""

        index = self.net.interface_index(self.interface_alias)
        if index is None:
            return None
        address = next(
            (ip for ip in self.net.preferred_ipv4_addresses(index) if not ipaddress.IPv4Address(ip).is_link_local),
            None,
        )
        gateway = next(
            (
                route.next_hop
                for route in self.net.ipv4_routes()
                if route.interface_index == index and route.prefix_length == 0 and route.next_hop != "0.0.0.0"
            ),
            None,
        )
        if address is None or gateway is None:
            return None
        return VpnLease(interface_index=index, ipv4=address, gateway=gateway)

    def session_established(self) -> bool:
        result = self._run(
            [self.vpncmd_path, "/CSV", "/CLIENT", "localhost", "/CMD", "AccountStatusGet", self.account_name],
            timeout=self.command_timeout,
        )
        return result.ok and SESSION_ID_RE.search(result.stdout + result.stderr) is not None

    def lease(self) -> VpnLease | None:
        lease = self.adapter_lease()
        if lease is None or not self.session_established():
            return None
        return lease

    @staticmethod
    def probe_tcp(relay: Relay, *, timeout: float) -> bool:
        try:
            with socket.create_connection((relay.ip, relay.port), timeout=timeout):
                return True
        except OSError:
            return False

    # --- state-changing operations -----------------------------------------

    def _vpncmd(self, *arguments: str, guard: Guard = _no_guard, timeout: float | None = None) -> CommandResult:
        guard()
        result = self._run(
            [self.vpncmd_path, "/CLIENT", "localhost", "/CMD", *arguments],
            timeout=min(self.command_timeout, timeout) if timeout else self.command_timeout,
        )
        if not result.ok:
            kind = LocalBusyError if result.exit_code == LOCAL_BUSY_EXIT_CODE else SoftEtherError
            suffix = f": {result.detail()}" if result.detail() else ""
            raise kind(f"{arguments[0]} 失败（退出码 {result.exit_code}）{suffix}", result)
        return result

    def disconnect(self, *, timeout: float, guard: Guard = _no_guard) -> bool:
        """Disconnect and wait until both the session and the lease are gone."""

        deadline = time.monotonic() + timeout
        guard()
        # "Not connected" also exits non-zero; the polling below is the truth.
        self._run(
            [self.vpncmd_path, "/CLIENT", "localhost", "/CMD", "AccountDisconnect", self.account_name],
            timeout=min(self.command_timeout, timeout),
        )
        while time.monotonic() < deadline:
            if self.adapter_lease() is None and not self.session_established():
                return True
            time.sleep(0.25)
        return False

    def connect(self, relay: Relay, *, timeout: float, guard: Guard = _no_guard) -> VpnLease:
        deadline = time.monotonic() + timeout

        def remaining() -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise SoftEtherError("连接超时")
            return left

        if not self.disconnect(timeout=min(remaining(), 15.0), guard=guard):
            raise LocalBusyError("上一个 SoftEther 会话没有释放")
        self._configure_account(relay, guard=guard, remaining=remaining)
        self._vpncmd("AccountConnect", self.account_name, guard=guard, timeout=remaining())
        while time.monotonic() < deadline:
            lease = self.lease()
            if lease is not None:
                return lease
            time.sleep(0.5)
        self.disconnect(timeout=5.0, guard=guard)
        raise SoftEtherError("握手后没有同时得到 SoftEther 会话和有效 IPv4 租约")

    def _account_exists(self, *, guard: Guard, remaining: Callable[[], float]) -> bool:
        listing = self._vpncmd("AccountList", guard=guard, timeout=remaining()).stdout
        return re.search(rf"(?im)(?:^|[|\s]){re.escape(self.account_name)}(?:$|[|\s])", listing) is not None

    def _configure_account(self, relay: Relay, *, guard: Guard, remaining: Callable[[], float]) -> None:
        exists = self._account_exists(guard=guard, remaining=remaining)
        # A UDP (NAT-T) relay is addressed with TCP port 0 plus PortUDP.
        server = f"/SERVER:{relay.ip}:{relay.port if relay.transport == 'tcp' else 0}"
        if not exists:
            self._vpncmd(
                "AccountCreate", self.account_name, server, "/HUB:VPNGATE", "/USERNAME:vpn",
                f"/NICNAME:{self.nic_name}", guard=guard, timeout=remaining(),
            )
            self._vpncmd(
                "AccountPasswordSet", self.account_name, "/PASSWORD:vpn", "/TYPE:standard",
                guard=guard, timeout=remaining(),
            )
        elif relay.transport == "tcp":
            self._vpncmd("AccountSet", self.account_name, server, "/HUB:VPNGATE", guard=guard, timeout=remaining())
        if relay.transport == "udp" or exists:
            # vpncmd has no NAT-T switch, but an exported account exposes
            # PortUDP.  Also reset it to 0 when rotating back to TCP, since
            # AccountSet alone would keep dialling the old UDP relay.
            self._set_udp_port(relay, guard=guard, remaining=remaining)
        # The keeper owns relay rotation; SoftEther must not retry forever.
        self._vpncmd("AccountRetrySet", self.account_name, "/NUM:0", "/INTERVAL:5", guard=guard, timeout=remaining())
        self._vpncmd("AccountStatusHide", self.account_name, guard=guard, timeout=remaining())

    def _set_udp_port(self, relay: Relay, *, guard: Guard, remaining: Callable[[], float]) -> None:
        with tempfile.TemporaryDirectory(prefix="tarkov-cis-vpn-") as directory:
            original = Path(directory) / "original.vpn"
            updated = Path(directory) / "updated.vpn"
            self._vpncmd("AccountExport", self.account_name, f"/SAVEPATH:{original}", guard=guard, timeout=remaining())
            source = original.read_text(encoding="utf-8-sig")
            fields: dict[str, tuple[str, object]] = {"PortUDP": ("uint", relay.port if relay.transport == "udp" else 0)}
            if relay.transport == "udp":
                fields.update({"Hostname": ("string", relay.ip), "Port": ("uint", 0)})
            changed = source
            for name, (kind, value) in fields.items():
                changed, count = re.subn(
                    rf"(?m)^(\s*{kind} {name} )\S+", lambda match: f"{match.group(1)}{value}", changed, count=1
                )
                if count != 1:
                    raise SoftEtherError(f"导出的 SoftEther 账户缺少 {name} 字段")
            if changed == source:
                return
            updated.write_text(changed, encoding="utf-8")
            self._vpncmd("AccountDelete", self.account_name, guard=guard, timeout=remaining())
            try:
                self._vpncmd("AccountImport", str(updated), guard=guard, timeout=remaining())
            except Exception:
                self._vpncmd("AccountImport", str(original), timeout=self.command_timeout)
                raise
