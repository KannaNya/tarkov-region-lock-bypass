"""Typed domain models shared by the Tarkov CIS connector components."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from ipaddress import ip_address
from types import MappingProxyType
from typing import Final, Mapping


COUNTRY_PRIORITIES: Final[Mapping[str, int]] = MappingProxyType(
    {
        "RU": 100,
        "UA": 95,
        "KZ": 90,
        "BY": 85,
        "AM": 80,
        "AZ": 75,
        "GE": 70,
        "MD": 65,
        "KG": 60,
        "TJ": 55,
        "TM": 50,
        "UZ": 45,
    }
)


def ensure_aware_utc(value: datetime) -> datetime:
    """Return a comparable UTC datetime, treating legacy naive values as UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ConnectionPhase(str, Enum):
    """Observable phases of one connector lifecycle."""

    DISCONNECTED = "disconnected"
    DISCOVERING = "discovering"
    CONNECTING = "connecting"
    VERIFYING_SESSION = "verifying_session"
    APPLYING_ROUTES = "applying_routes"
    READY = "ready"
    COOLING = "cooling"
    DISCONNECTING = "disconnecting"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Relay:
    """One concrete TCP endpoint exposed by a VPN Gate volunteer relay."""

    host_name: str
    ip: str
    port: int
    country_short: str
    country_long: str = ""
    score: int = 0
    ping: int = 9999
    speed_mbps: float = 0.0
    sessions: int = 0
    source: str = "HttpsApi"
    source_priority: int = 2
    verified_at: datetime | None = None

    def __post_init__(self) -> None:
        canonical_ip = ip_address(self.ip.strip()).compressed
        if not 1 <= int(self.port) <= 65_535:
            raise ValueError(f"relay port is outside 1..65535: {self.port}")
        country = self.country_short.strip().upper()
        if not country:
            raise ValueError("relay country_short cannot be empty")
        if self.verified_at is not None:
            object.__setattr__(self, "verified_at", ensure_aware_utc(self.verified_at))
        object.__setattr__(self, "host_name", self.host_name.strip() or canonical_ip)
        object.__setattr__(self, "ip", canonical_ip)
        object.__setattr__(self, "port", int(self.port))
        object.__setattr__(self, "country_short", country)
        object.__setattr__(self, "country_long", self.country_long.strip())
        object.__setattr__(self, "score", int(self.score))
        object.__setattr__(self, "ping", max(0, int(self.ping)))
        object.__setattr__(self, "speed_mbps", max(0.0, float(self.speed_mbps)))
        object.__setattr__(self, "sessions", max(0, int(self.sessions)))
        object.__setattr__(self, "source_priority", int(self.source_priority))

    @property
    def endpoint(self) -> str:
        address = f"[{self.ip}]" if ":" in self.ip else self.ip
        return f"{address}:{self.port}"

    @property
    def identity(self) -> str:
        """Stable relay identity shared by alternate ports on the same host."""

        return f"ip:{self.ip}"

    @property
    def priority(self) -> int:
        return COUNTRY_PRIORITIES.get(self.country_short, 0)


@dataclass(frozen=True, slots=True)
class FailureRecord:
    """Latest observed failure for a concrete relay endpoint."""

    endpoint: str
    failed_at: datetime
    reason: str = ""
    country_short: str = ""
    host_name: str = ""

    def __post_init__(self) -> None:
        endpoint = self.endpoint.strip().lower()
        if not endpoint:
            raise ValueError("failure endpoint cannot be empty")
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "failed_at", ensure_aware_utc(self.failed_at))
        object.__setattr__(self, "country_short", self.country_short.strip().upper())
        object.__setattr__(self, "host_name", self.host_name.strip())


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """A bounded connection batch plus the cooldown evidence behind it."""

    candidates: tuple[Relay, ...] = ()
    cooling_until: Mapping[str, datetime] = field(default_factory=dict)
    used_cooling_fallback: bool = False
    retry_at: datetime | None = None

    def __post_init__(self) -> None:
        normalized = {
            endpoint.lower(): ensure_aware_utc(value)
            for endpoint, value in self.cooling_until.items()
        }
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "cooling_until", MappingProxyType(normalized))
        if self.retry_at is not None:
            object.__setattr__(self, "retry_at", ensure_aware_utc(self.retry_at))
