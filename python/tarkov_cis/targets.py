"""Authorization hostnames (config + recent EFT logs) and their current IPv4s."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import re
import socket
from typing import Callable, Iterable

# Deliberately narrow: CDN, launcher and Raid endpoints must stay on the local
# default route, so only lobby, gw-pvp and WSN hosts are ever routed.
AUTHORIZATION_HOST_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?P<host>(?:lobby|gw-pvp(?:-season)?|wsn(?:-[a-z0-9]+)+)"
    r"\.escapefromtarkov\.(?:ru|com))(?![A-Za-z0-9.-])",
    re.IGNORECASE,
)

# Relative locations probed on every fixed drive in addition to the config.
CONVENTIONAL_LOG_DIRS = (
    Path("GAME/Tarkov/Logs"),
    Path("Battlestate Games/EFT/Logs"),
    Path("Games/EFT/Logs"),
    Path("Program Files (x86)/Escape from Tarkov/Logs"),
)

Resolver = Callable[..., list]


def is_authorization_host(value: str) -> bool:
    return AUTHORIZATION_HOST_RE.fullmatch(value.strip().rstrip(".")) is not None


def fixed_drives() -> list[Path]:
    if os.name != "nt":
        return []
    import ctypes

    get_type = ctypes.windll.kernel32.GetDriveTypeW
    return [Path(f"{letter}:/") for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ" if get_type(f"{letter}:\\") == 3]


def log_roots(configured: Iterable[str], *, drives: Iterable[Path] | None = None) -> tuple[Path, ...]:
    """Configured log roots plus existing conventional ones on fixed drives.

    A configured path also seeds the same relative path on other drives, so a
    game moved to another disk is still found.
    """

    roots = {Path(value) for value in configured}
    suffixes = set(CONVENTIONAL_LOG_DIRS)
    suffixes.update(Path(*root.parts[1:]) for root in roots if root.anchor)
    for drive in fixed_drives() if drives is None else drives:
        for suffix in suffixes:
            candidate = Path(drive) / suffix
            if candidate.is_dir():
                roots.add(candidate)
    return tuple(sorted(roots, key=str))


def recent_log_files(roots: Iterable[Path], *, max_age_days: int, max_files: int) -> list[Path]:
    """Newest *.log files under *roots* modified within *max_age_days*."""

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).timestamp()
    found: list[tuple[float, Path]] = []
    for root in roots:
        if not Path(root).is_dir():
            continue
        try:
            for path in Path(root).rglob("*.log"):
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                if modified >= cutoff:
                    found.append((modified, path))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:max_files]]


def read_tail(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - max_bytes))
            return handle.read(max_bytes).decode("utf-8", errors="ignore")
    except OSError:
        return ""


def discover_hosts(
    static_hosts: Iterable[str],
    roots: Iterable[Path],
    *,
    max_age_days: int = 7,
    max_files: int = 50,
    max_bytes_per_file: int = 4 * 1024 * 1024,
) -> tuple[str, ...]:
    """Static hosts plus lobby/WSN/gw-pvp hostnames mentioned in recent logs.

    Literal IPs in logs are never returned, so Raid server addresses cannot
    leak into the VPN route set.
    """

    hosts = {h.strip().rstrip(".").lower() for h in static_hosts if is_authorization_host(h)}
    for path in recent_log_files(roots, max_age_days=max_age_days, max_files=max_files):
        hosts.update(m.group("host").lower() for m in AUTHORIZATION_HOST_RE.finditer(read_tail(path, max_bytes_per_file)))
    return tuple(sorted(hosts))


def resolve_ipv4(hosts: Iterable[str], *, timeout: float = 5.0, resolver: Resolver = socket.getaddrinfo) -> tuple[str, ...]:
    """Resolve all hosts concurrently within one wall-clock *timeout*."""

    hosts = tuple(hosts)
    if not hosts:
        return ()
    addresses: set[str] = set()
    executor = ThreadPoolExecutor(max_workers=min(16, len(hosts)), thread_name_prefix="TarkovCisDns")
    try:
        futures = [executor.submit(resolver, host, 443, socket.AF_INET, socket.SOCK_STREAM) for host in hosts]
        done, _ = wait(futures, timeout=timeout)
        for future in done:
            try:
                records = future.result()
            except OSError:
                continue
            for record in records:
                try:
                    addresses.add(str(ipaddress.IPv4Address(record[4][0])))
                except (IndexError, TypeError, ValueError):
                    continue
    finally:
        # Never block on a hung resolver thread; it finishes in the background.
        executor.shutdown(wait=False, cancel_futures=True)
    return tuple(sorted(addresses, key=ipaddress.IPv4Address))


def authorization_ips(static_hosts: Iterable[str], configured_roots: Iterable[str]) -> tuple[str, ...]:
    return resolve_ipv4(discover_hosts(static_hosts, log_roots(configured_roots)))
