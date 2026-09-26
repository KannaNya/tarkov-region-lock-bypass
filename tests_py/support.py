"""Shared helpers: put the package on sys.path and provide small fakes."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from tarkov_cis.models import Relay  # noqa: E402
from tarkov_cis.winapi.iphlpapi import InterfaceInfo, Route  # noqa: E402


def relay(ip: str, port: int = 443, country: str = "RU", **values) -> Relay:
    values.setdefault("source", "HttpsApi")
    return Relay(ip=ip, port=port, country=country, **values)


class FakeNet:
    """In-memory stand-in for tarkov_cis.winapi.iphlpapi."""

    def __init__(self) -> None:
        self.aliases: dict[str, int] = {}
        self.addresses: dict[int, list[str]] = {}
        self.routes: list[Route] = []
        self.metrics: dict[int, int] = {}
        self.automatic: dict[int, bool] = {}
        self.calls: list[tuple] = []

    def interface_index(self, alias: str) -> int | None:
        return self.aliases.get(alias)

    def preferred_ipv4_addresses(self, index: int) -> list[str]:
        return list(self.addresses.get(index, []))

    def ipv4_routes(self) -> list[Route]:
        return list(self.routes)

    def interface_info(self, index: int) -> InterfaceInfo:
        return InterfaceInfo(index, True, self.metrics.get(index, 1), self.automatic.get(index, False))

    def set_interface_metric(self, index: int, metric: int) -> None:
        self.calls.append(("metric", index, metric))
        self.metrics[index] = metric
        self.automatic[index] = False

    def add_route(self, destination, prefix_length, next_hop, index, *, metric=1) -> bool:
        self.calls.append(("add", destination, index))
        route = Route(destination, prefix_length, next_hop, index, metric)
        if any((r.prefix, r.next_hop, r.interface_index) == (route.prefix, next_hop, index) for r in self.routes):
            return False
        self.routes.append(route)
        return True

    def delete_route(self, destination, prefix_length, next_hop, index) -> bool:
        self.calls.append(("delete", destination, index))
        before = len(self.routes)
        self.routes = [
            r for r in self.routes
            if (r.destination, r.prefix_length, r.next_hop, r.interface_index) != (destination, prefix_length, next_hop, index)
        ]
        return len(self.routes) != before

    def add_default(self, index: int, gateway: str, metric: int = 0) -> None:
        self.routes.append(Route("0.0.0.0", 0, gateway, index, metric))
