"""Bounded discovery of EFT authorization hostnames from local logs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import queue
import re
import socket
import threading
import time
from typing import Callable, Iterable, Iterator


DEFAULT_AUTHORIZATION_HOSTS = (
    "gw-pvp.escapefromtarkov.ru",
    "gw-pvp.escapefromtarkov.com",
    "gw-pvp-season.escapefromtarkov.ru",
    "lobby.escapefromtarkov.ru",
)

# Do not broaden this to a generic escapefromtarkov.com matcher.  CDN,
# launcher and Raid endpoints intentionally stay on the Japanese default path.
AUTHORIZATION_HOST_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?P<host>"
    r"(?:lobby|gw-pvp(?:-season)?|wsn(?:-[a-z0-9]+)+)"
    r"\.escapefromtarkov\.(?:ru|com))"
    r"(?![A-Za-z0-9.-])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AuthorizationTarget:
    ip: str
    hostnames: tuple[str, ...]


Resolver = Callable[..., list[tuple]]


def is_authorization_host(value: str) -> bool:
    return AUTHORIZATION_HOST_RE.fullmatch(value.strip().rstrip(".")) is not None


def _recent_log_files(
    roots: Iterable[str | Path], *, max_age_days: int, max_files: int
) -> Iterator[Path]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, max_age_days))
    found: list[tuple[float, Path]] = []
    for root_value in roots:
        root = Path(root_value)
        if not root.is_dir():
            continue
        try:
            iterator = root.rglob("*.log")
            for path in iterator:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                if modified >= cutoff:
                    found.append((stat.st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(found, key=lambda item: item[0], reverse=True)[:max_files]:
        yield path


def _read_tail(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
    except OSError:
        return ""
    return data.decode("utf-8", errors="ignore")


def discover_authorization_hosts(
    roots: Iterable[str | Path],
    *,
    static_hosts: Iterable[str] = DEFAULT_AUTHORIZATION_HOSTS,
    max_age_days: int = 7,
    max_files: int = 50,
    max_bytes_per_file: int = 4 * 1024 * 1024,
) -> tuple[str, ...]:
    """Return only lobby, WSN and gw-pvp hostnames.

    Literal server IPs in logs are never returned, which prevents dynamic Raid
    addresses from being promoted into VPN routes by time correlation alone.
    """

    hosts = {
        value.strip().rstrip(".").lower()
        for value in static_hosts
        if is_authorization_host(value)
    }
    if max_files <= 0 or max_bytes_per_file <= 0:
        return tuple(sorted(hosts))

    for path in _recent_log_files(
        roots, max_age_days=max_age_days, max_files=max_files
    ):
        for match in AUTHORIZATION_HOST_RE.finditer(
            _read_tail(path, max_bytes_per_file)
        ):
            hosts.add(match.group("host").lower())
    return tuple(sorted(hosts))


def resolve_authorization_targets(
    hosts: Iterable[str],
    *,
    resolver: Resolver = socket.getaddrinfo,
    timeout: float = 5.0,
    max_hosts: int = 64,
) -> tuple[AuthorizationTarget, ...]:
    """Resolve a bounded set of A records within one total wall-clock limit."""

    valid_hosts = tuple(
        sorted(
            {
                value.strip().rstrip(".").lower()
                for value in hosts
                if is_authorization_host(value.strip().rstrip(".").lower())
            }
        )[: max(0, max_hosts)]
    )
    if not valid_hosts or timeout <= 0:
        return ()

    responses: queue.Queue[tuple[str, object]] = queue.Queue()

    def resolve_one(host: str) -> None:
        try:
            records: object = resolver(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        except Exception as exc:
            records = exc
        responses.put((host, records))

    for host in valid_hosts:
        threading.Thread(
            target=resolve_one,
            args=(host,),
            name=f"TarkovCisDns-{host}",
            daemon=True,
        ).start()

    by_ip: dict[str, set[str]] = {}
    deadline = time.monotonic() + timeout
    received = 0
    while received < len(valid_hosts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            host, records = responses.get(timeout=remaining)
        except queue.Empty:
            break
        received += 1
        if isinstance(records, Exception):
            continue
        for record in records:
            try:
                address = str(ipaddress.IPv4Address(record[4][0]))
            except (IndexError, TypeError, ValueError):
                continue
            by_ip.setdefault(address, set()).add(host)
    return tuple(
        AuthorizationTarget(ip=ip, hostnames=tuple(sorted(names)))
        for ip, names in sorted(by_ip.items(), key=lambda item: ipaddress.IPv4Address(item[0]))
    )
