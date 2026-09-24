"""Deterministic, country-diverse relay selection and cooldown recovery."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .models import COUNTRY_PRIORITIES, FailureRecord, Relay, SelectionResult, ensure_aware_utc


def _now(value: datetime | None) -> datetime:
    return ensure_aware_utc(value or datetime.now(timezone.utc))


def _country_key(country: str) -> tuple[int, str]:
    return (-COUNTRY_PRIORITIES.get(country, 0), country)


def _relay_key(relay: Relay) -> tuple[int, int, float, int, int, str]:
    return (
        -relay.source_priority,
        -(1 if relay.sessions > 0 else 0),
        -relay.speed_mbps,
        -relay.score,
        relay.ping,
        relay.endpoint,
    )


def deduplicate_relays(relays: Iterable[Relay]) -> tuple[Relay, ...]:
    """Keep the strongest observation for each concrete endpoint."""

    result: dict[str, Relay] = {}
    for relay in relays:
        previous = result.get(relay.endpoint)
        if previous is None or _relay_key(relay) < _relay_key(previous):
            result[relay.endpoint] = relay
    return tuple(result.values())


def _independent_first(relays: Iterable[Relay], limit: int | None = None) -> list[Relay]:
    primary: list[Relay] = []
    alternate_ports: list[Relay] = []
    seen: set[str] = set()
    for relay in sorted(relays, key=_relay_key):
        if relay.identity in seen:
            alternate_ports.append(relay)
        else:
            seen.add(relay.identity)
            primary.append(relay)
    ordered = primary + alternate_ports
    return ordered if limit is None else ordered[: max(0, limit)]


def country_round_robin(relays: Iterable[Relay], per_country_limit: int) -> tuple[Relay, ...]:
    """Take RU, then UA, then each remaining CIS country once per round."""

    if per_country_limit <= 0:
        return ()
    groups: dict[str, list[Relay]] = defaultdict(list)
    for relay in deduplicate_relays(relays):
        groups[relay.country_short].append(relay)
    ordered_groups = [
        _independent_first(groups[country], per_country_limit)
        for country in sorted(groups, key=_country_key)
    ]
    selected: list[Relay] = []
    for round_index in range(per_country_limit):
        for candidates in ordered_groups:
            if round_index < len(candidates):
                selected.append(candidates[round_index])
    return tuple(selected)


def _latest_failures(failures: Iterable[FailureRecord]) -> dict[str, FailureRecord]:
    result: dict[str, FailureRecord] = {}
    for failure in failures:
        previous = result.get(failure.endpoint)
        if previous is None or failure.failed_at > previous.failed_at:
            result[failure.endpoint] = failure
    return result


def merge_recent_known_good(
    live_relays: Iterable[Relay],
    known_good: Iterable[Relay],
    *,
    now: datetime | None = None,
    lifetime: timedelta = timedelta(hours=48),
    per_country_limit: int = 3,
) -> tuple[Relay, ...]:
    """Add recent verified endpoints missing from the current live snapshot."""

    current_time = _now(now)
    live = list(deduplicate_relays(live_relays))
    present = {relay.endpoint for relay in live}
    grouped: dict[str, list[Relay]] = defaultdict(list)
    for relay in known_good:
        if relay.endpoint in present or relay.verified_at is None:
            continue
        if current_time - relay.verified_at > lifetime:
            continue
        grouped[relay.country_short].append(relay)
    for country in sorted(grouped, key=_country_key):
        recent = sorted(
            grouped[country],
            key=lambda relay: (-(relay.verified_at or current_time).timestamp(), relay.endpoint),
        )
        for relay in recent[: max(0, per_country_limit)]:
            live.append(relay)
            present.add(relay.endpoint)
    return deduplicate_relays(live)


def select_relay_candidates(
    relays: Iterable[Relay],
    failures: Iterable[FailureRecord] = (),
    *,
    now: datetime | None = None,
    per_country_limit: int = 10,
    total_limit: int = 20,
    cooldown: timedelta = timedelta(minutes=15),
) -> SelectionResult:
    """Build one bounded attempt batch while excluding cooling endpoints."""

    current_time = _now(now)
    latest = _latest_failures(failures)
    cooling_until = {
        endpoint: failure.failed_at + cooldown
        for endpoint, failure in latest.items()
        if failure.failed_at + cooldown > current_time
    }
    eligible = [
        relay
        for relay in deduplicate_relays(relays)
        if relay.endpoint not in cooling_until
    ]

    live = [relay for relay in eligible if relay.source != "RecentKnownGood"]
    known_by_country: dict[str, list[Relay]] = defaultdict(list)
    for relay in eligible:
        if relay.source == "RecentKnownGood":
            known_by_country[relay.country_short].append(relay)
    known: list[Relay] = []
    for country in sorted(known_by_country, key=_country_key):
        known.extend(
            sorted(
                known_by_country[country],
                key=lambda relay: (
                    -(relay.verified_at.timestamp() if relay.verified_at else 0),
                    relay.endpoint,
                ),
            )[:3]
        )

    round_robin = country_round_robin(live + known, per_country_limit)
    independent: list[Relay] = []
    alternate_ports: list[Relay] = []
    seen: set[str] = set()
    for relay in round_robin:
        if relay.identity in seen:
            alternate_ports.append(relay)
        else:
            seen.add(relay.identity)
            independent.append(relay)
    candidates = tuple((independent + alternate_ports)[: max(0, total_limit)])
    return SelectionResult(candidates=candidates, cooling_until=cooling_until)


def select_cooling_fallback(
    relays: Iterable[Relay],
    failures: Iterable[FailureRecord],
    *,
    now: datetime | None = None,
    limit: int = 3,
    minimum_age: timedelta = timedelta(minutes=2),
) -> tuple[Relay, ...]:
    """Retry a small set of oldest failures after the all-cooling grace period."""

    if limit <= 0:
        return ()
    current_time = _now(now)
    latest = _latest_failures(failures)
    ranked = [
        (latest[relay.endpoint].failed_at, relay)
        for relay in deduplicate_relays(relays)
        if relay.endpoint in latest
        and current_time - latest[relay.endpoint].failed_at >= minimum_age
    ]
    ranked.sort(
        key=lambda item: (
            item[0],
            _country_key(item[1].country_short),
            _relay_key(item[1]),
        )
    )
    independent: list[Relay] = []
    alternate_ports: list[Relay] = []
    seen: set[str] = set()
    for _, relay in ranked:
        if relay.identity in seen:
            alternate_ports.append(relay)
        else:
            seen.add(relay.identity)
            independent.append(relay)
    return tuple((independent + alternate_ports)[:limit])


def next_cooling_fallback_at(
    relays: Iterable[Relay],
    failures: Iterable[FailureRecord],
    *,
    minimum_age: timedelta = timedelta(minutes=2),
) -> datetime | None:
    """Return the first controlled retry time for endpoints in this catalog."""

    present = {relay.endpoint for relay in relays}
    latest = _latest_failures(failures)
    retry_times = [
        failure.failed_at + minimum_age
        for endpoint, failure in latest.items()
        if endpoint in present
    ]
    return min(retry_times, default=None)


def plan_failover_cycle(
    relays: Iterable[Relay],
    failures: Iterable[FailureRecord] = (),
    *,
    now: datetime | None = None,
    per_country_limit: int = 10,
    total_limit: int = 20,
    cooldown: timedelta = timedelta(minutes=15),
    fallback_age: timedelta = timedelta(minutes=2),
    fallback_limit: int = 3,
) -> SelectionResult:
    """Choose regular candidates, or a bounded fallback if all are cooling."""

    relay_list = deduplicate_relays(relays)
    failure_list = tuple(failures)
    regular = select_relay_candidates(
        relay_list,
        failure_list,
        now=now,
        per_country_limit=per_country_limit,
        total_limit=total_limit,
        cooldown=cooldown,
    )
    if regular.candidates:
        return regular
    fallback = select_cooling_fallback(
        relay_list,
        failure_list,
        now=now,
        limit=min(max(0, fallback_limit), max(0, total_limit)),
        minimum_age=fallback_age,
    )
    return SelectionResult(
        candidates=fallback,
        cooling_until=regular.cooling_until,
        used_cooling_fallback=bool(fallback),
        retry_at=None if fallback else next_cooling_fallback_at(
            relay_list,
            failure_list,
            minimum_age=fallback_age,
        ),
    )
