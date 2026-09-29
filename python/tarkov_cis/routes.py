"""The /32 authorization routes this tool owns, and nothing else.

Split tunnelling rests on two facts:

* each authorization IP gets a /32 route via the VPN gateway, which wins over
  any default route by longest-prefix match regardless of metric;
* the VPN adapter's interface metric is pinned very high, so the default
  route SoftEther's DHCP installs can never outrank the physical network,
  even if it is re-added mid-match while the keeper is not looking.

Routes are recorded in routes.json before the keeper relies on them so a
crashed keeper's routes can still be removed exactly.  Routes that already
existed are never claimed, so they are never deleted either.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
from typing import Callable, Iterable

from .models import VpnLease

VPN_INTERFACE_METRIC = 9000
Guard = Callable[[], None]


def _no_guard() -> None:
    return None


class RouteError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OwnedRoute:
    ip: str
    interface_index: int
    gateway: str


def _routable(ip: str) -> str:
    address = ipaddress.IPv4Address(ip)
    if address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast:
        raise ValueError(f"unsafe authorization target: {address}")
    return str(address)


class RouteManager:
    def __init__(self, state_path: Path, *, net=None) -> None:
        self.state_path = state_path
        self._net = net
        self._owned: set[OwnedRoute] = self._load()

    @property
    def net(self):
        if self._net is None:
            from .winapi import iphlpapi

            self._net = iphlpapi
        return self._net

    @property
    def owned(self) -> tuple[OwnedRoute, ...]:
        return tuple(sorted(self._owned, key=lambda route: (ipaddress.IPv4Address(route.ip), route.interface_index)))

    def _load(self) -> set[OwnedRoute]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
            # Version 1 (pre-refactor) stored the same triples as managed_routes.
            entries = raw.get("routes", raw.get("managed_routes", ()))
            return {
                OwnedRoute(_routable(item["ip"]), int(item["interface_index"]), str(ipaddress.IPv4Address(item["gateway"])))
                for item in entries
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # A missing or corrupt record is never a license to delete routes.
            return set()

    def _save(self) -> None:
        if not self._owned:
            self.state_path.unlink(missing_ok=True)
            return
        payload = {
            "version": 2,
            "routes": [
                {"ip": route.ip, "interface_index": route.interface_index, "gateway": route.gateway}
                for route in self.owned
            ],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def pin_vpn_metric(self, lease: VpnLease) -> None:
        info = self.net.interface_info(lease.interface_index)
        if info.metric != VPN_INTERFACE_METRIC or info.automatic_metric:
            self.net.set_interface_metric(lease.interface_index, VPN_INTERFACE_METRIC)

    def check_physical_default(self, lease: VpnLease) -> None:
        """Fail loudly if the VPN would still carry ordinary traffic."""

        defaults = [route for route in self.net.ipv4_routes() if route.prefix_length == 0]
        if not defaults:
            return
        metrics = {}
        for route in defaults:
            if route.interface_index not in metrics:
                try:
                    metrics[route.interface_index] = self.net.interface_info(route.interface_index).metric
                except OSError:
                    metrics[route.interface_index] = 0
        best = min(defaults, key=lambda route: route.metric + metrics[route.interface_index])
        if best.interface_index == lease.interface_index and len({r.interface_index for r in defaults}) > 1:
            raise RouteError("VPN 网卡仍是默认出口；为避免全局代理，已停止添加路由")

    def sync(self, ips: Iterable[str], lease: VpnLease, *, guard: Guard = _no_guard) -> None:
        desired = {_routable(ip) for ip in ips}
        if not desired:
            raise RouteError("没有可用的鉴权目标 IP")
        guard()
        self.pin_vpn_metric(lease)
        self.check_physical_default(lease)
        for route in self.owned:
            if route.ip not in desired or (route.interface_index, route.gateway) != (lease.interface_index, lease.gateway):
                guard()
                self._remove(route)
        for ip in sorted(desired, key=ipaddress.IPv4Address):
            route = OwnedRoute(ip, lease.interface_index, lease.gateway)
            if route in self._owned:
                continue
            guard()
            if self.net.add_route(ip, 32, lease.gateway, lease.interface_index):
                self._owned.add(route)
                self._save()

    def cleanup(self, *, guard: Guard = _no_guard) -> None:
        errors = []
        for route in self.owned:
            guard()
            try:
                self._remove(route)
            except OSError as exc:
                errors.append(exc)
        if errors:
            raise RouteError(f"{len(errors)} 条临时路由删除失败: {errors[0]}")

    def _remove(self, route: OwnedRoute) -> None:
        # Already gone (e.g. the adapter went down) counts as removed.
        self.net.delete_route(route.ip, 32, route.gateway, route.interface_index)
        self._owned.discard(route)
        self._save()
