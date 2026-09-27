"""Relay candidate selection, failure cooldown and the known-good cache."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from typing import Callable, Iterable

from .models import Relay


SOURCE_RANK = {"KnownGood": 0, "NativeCatalog": 1}


def _quality(relay: Relay) -> tuple:
    # A relay that really connected recently is the best bet; native catalog
    # entries are fresher than the HTTPS snapshot; then prefer relays with
    # live sessions, reported speed and low ping.
    return (
        SOURCE_RANK.get(relay.source, 2),
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


class KnownGood:
    """Relays that really connected, persisted so a restart can reuse them.

    VPN Gate catalogs are small snapshots; a relay that worked an hour ago
    is often missing from the next one while still being up.
    """

    def __init__(self, path: Path, *, lifetime_hours: float, clock: Callable[[], datetime] | None = None) -> None:
        self.path = path
        self.lifetime = timedelta(hours=lifetime_hours)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._verified: dict[str, tuple[Relay, datetime]] = self._load()

    def _load(self) -> dict[str, tuple[Relay, datetime]]:
        try:
            entries = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        result: dict[str, tuple[Relay, datetime]] = {}
        for item in entries if isinstance(entries, list) else ():
            try:
                verified = datetime.fromisoformat(item.pop("verified_at"))
                if verified.tzinfo is None:
                    verified = verified.replace(tzinfo=timezone.utc)
                # Pre-refactor files named the country field country_short.
                item.setdefault("country", item.pop("country_short", ""))
                fields = {k: v for k, v in item.items() if k in Relay.__dataclass_fields__}
                relay = Relay(**fields)
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            result[relay.endpoint] = (relay, verified)
        return result

    def _save(self) -> None:
        payload = [
            {**asdict(relay), "verified_at": verified.isoformat()}
            for relay, verified in sorted(self._verified.values(), key=lambda item: item[1])[-100:]
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def remember(self, relay: Relay) -> None:
        self._verified[relay.endpoint] = (replace(relay, source=""), self._clock())
        try:
            self._save()
        except OSError:
            pass

    def fresh(self) -> list[Relay]:
        cutoff = self._clock() - self.lifetime
        return [
            replace(relay, source="KnownGood")
            for relay, verified in sorted(self._verified.values(), key=lambda item: item[1], reverse=True)
            if verified >= cutoff
        ]

    def merge(self, relays: Iterable[Relay]) -> list[Relay]:
        """Live relays plus recent known-good ones, known-good taking precedence."""

        merged = {relay.endpoint: relay for relay in relays}
        merged.update({relay.endpoint: relay for relay in self.fresh()})
        return list(merged.values())