"""Relay candidate selection with per-endpoint failure cooldown."""

from __future__ import annotations

from collections import defaultdict
import time
from typing import Callable, Iterable

from .models import Relay


def _quality(relay: Relay) -> tuple:
    # Native catalog entries are fresher than the HTTPS snapshot; then prefer
    # relays with live sessions, reported speed and low ping.
    return (
        relay.source != "NativeCatalog",
        relay.sessions == 0,
        -relay.speed_mbps,
        relay.ping,
        -relay.score,
        relay.endpoint,
    )


def order_candidates(
    relays: Iterable[Relay], *, per_country: int, total: int
) -> tuple[Relay, ...]:
    """Round-robin across CIS countries (RU first), distinct IPs before extra ports.

    Each round takes the next-best relay of every country, so a dead RU pool
    cannot starve UA/KZ/BY.  A second port of an already chosen IP only fills
    remaining slots at the end.
    """

    best: dict[str, Relay] = {}
    for relay in relays:
        if relay.endpoint not in best or _quality(relay) < _quality(best[relay.endpoint]):
            best[relay.endpoint] = relay

    by_country: dict[str, list[Relay]] = defaultdict(list)
    for relay in sorted(best.values(), key=_quality):
        by_country[relay.country].append(relay)
    countries = sorted(by_country, key=lambda code: (by_country[code][0].country_rank, code))

    primary: list[Relay] = []
    extra_ports: list[Relay] = []
    seen_ips: set[str] = set()
    for round_index in range(per_country):
        for country in countries:
            pool = by_country[country]
            if round_index < len(pool):
                relay = pool[round_index]
                (extra_ports if relay.ip in seen_ips else primary).append(relay)
                seen_ips.add(relay.ip)
    return tuple((primary + extra_ports)[:total])


class FailureCooldown:
    """Remember failed endpoints so the next round tries fresh ones first."""

    def __init__(self, cooldown_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._failed_at: dict[str, float] = {}

    def record_failure(self, relay: Relay) -> None:
        self._failed_at[relay.endpoint] = self._clock()

    def record_success(self, relay: Relay) -> None:
        self._failed_at.pop(relay.endpoint, None)

    def is_cooling(self, relay: Relay) -> bool:
        failed = self._failed_at.get(relay.endpoint)
        return failed is not None and self._clock() - failed < self.cooldown_seconds

    def filter(self, relays: Iterable[Relay]) -> list[Relay]:
        """Drop cooling endpoints; if all are cooling, retry oldest failures first."""

        relays = list(relays)
        fresh = [relay for relay in relays if not self.is_cooling(relay)]
        if fresh:
            return fresh
        return sorted(relays, key=lambda relay: self._failed_at.get(relay.endpoint, 0.0))
