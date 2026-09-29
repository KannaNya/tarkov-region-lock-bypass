"""Shared value types."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress

# CIS exit countries in preference order; RU is tried first.
COUNTRY_PRIORITY = ("RU", "UA", "KZ", "BY", "AM", "AZ", "GE", "MD", "KG", "TJ", "TM", "UZ")


@dataclass(frozen=True, slots=True)
class Relay:
    """One concrete SoftEther endpoint: TCP (SSL port) or UDP NAT traversal."""

    ip: str
    port: int
    country: str
    host_name: str = ""
    transport: str = "tcp"
    speed_mbps: float = 0.0
    ping: int = 9999
    score: int = 0
    sessions: int = 0
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ip", str(ipaddress.IPv4Address(self.ip.strip())))
        object.__setattr__(self, "country", self.country.strip().upper())
        if self.transport not in ("tcp", "udp"):
            raise ValueError(f"unsupported relay transport: {self.transport}")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError(f"relay port out of range: {self.port}")

    @property
    def endpoint(self) -> str:
        prefix = "udp://" if self.transport == "udp" else ""
        return f"{prefix}{self.ip}:{self.port}"

    @property
    def country_rank(self) -> int:
        try:
            return COUNTRY_PRIORITY.index(self.country)
        except ValueError:
            return len(COUNTRY_PRIORITY)


@dataclass(frozen=True, slots=True)
class VpnLease:
    """A verified SoftEther session: the adapter has an address and a gateway."""

    interface_index: int
    ipv4: str
    gateway: str
