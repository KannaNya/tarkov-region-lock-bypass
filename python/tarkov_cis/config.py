"""config.json loading.

The file keeps its original PascalCase keys.  Keys this version no longer
uses (e.g. HealthFailureThreshold, DisconnectAtRaid) are ignored rather than
rejected, so an existing config.json keeps working after an upgrade.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping


def project_root() -> Path:
    """Writable bundle root for a frozen build, repository root otherwise."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "TarkovCIS"


DEFAULT_CONFIG_PATH = project_root() / "config.json"
EXAMPLE_CONFIG_PATH = project_root() / "config.example.json"

DEFAULT_TARGET_HOSTS = (
    "gw-pvp.escapefromtarkov.ru",
    "gw-pvp.escapefromtarkov.com",
    "gw-pvp-season.escapefromtarkov.ru",
    "gw-pvp-season.escapefromtarkov.com",
    "lobby.escapefromtarkov.ru",
    "lobby.escapefromtarkov.com",
)


@dataclass(frozen=True, slots=True)
class Config:
    task_name: str = "Tarkov-CIS-RouteKeeper"
    vpn_interface_alias: str = "VPN - VPN Client"
    account_name: str = "Tarkov-CIS-PlayOnly"
    vpncmd_path: str = r"C:\Program Files\SoftEther VPN Client\vpncmd_x64.exe"
    nic_name: str = "VPN"
    native_catalog_path: str = ""
    native_catalog_max_age_hours: int = 24
    # Seconds between health checks while connected.
    refresh_seconds: int = 30
    # Retry delay after a failed failover round, doubling up to the maximum.
    failed_cycle_retry_seconds: int = 10
    failed_cycle_backoff_max_seconds: int = 10
    # Consecutive checks without a session/lease before switching relays.
    session_failure_threshold: int = 3
    pause_during_raid: bool = True
    game_phase_max_age_days: int = 2
    game_phase_max_files: int = 24
    game_phase_max_bytes_per_file: int = 256 * 1024
    connect_timeout_seconds: int = 18
    disconnect_wait_seconds: int = 15
    failover_timeout_seconds: int = 180
    max_candidates_per_country: int = 10
    max_candidates_total: int = 20
    tcp_probe_timeout_milliseconds: int = 1500
    discovery_timeout_seconds: int = 15
    failure_cooldown_minutes: int = 15
    game_log_roots: tuple[str, ...] = ()
    target_hosts: tuple[str, ...] = DEFAULT_TARGET_HOSTS

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "Config":
        if not isinstance(data, Mapping):
            raise ValueError("config.json 的根节点必须是 JSON 对象")
        values: dict[str, Any] = {}
        for field in fields(cls):
            key = _pascal(field.name)
            if key in data:
                raw = data[key]
            elif field.name in data:
                raw = data[field.name]
            else:
                continue
            values[field.name] = _coerce(field.name, raw, field.default)
        return cls(**values)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with Path(path).open("r", encoding="utf-8-sig") as handle:
            return cls.from_mapping(json.load(handle))

    @property
    def native_catalog(self) -> Path:
        if self.native_catalog_path:
            return Path(self.native_catalog_path)
        return Path(self.vpncmd_path).parent / "VPNGate.dat"


def _pascal(name: str) -> str:
    special = {"vpncmd_path": "VpnCmdPath"}
    return special.get(name) or "".join(part.capitalize() for part in name.split("_"))


def _coerce(name: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
        raise ValueError(f"{_pascal(name)} 必须是 true 或 false")
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"{_pascal(name)} 必须是整数")
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError(f"{_pascal(name)} 必须是整数") from exc
        if number < 1:
            raise ValueError(f"{_pascal(name)} 必须大于 0")
        return number
    if isinstance(default, tuple):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{_pascal(name)} 必须是字符串数组")
        return tuple(item.strip() for item in value if item.strip())
    text = str(value).strip()
    if not text and default:
        raise ValueError(f"{_pascal(name)} 不能为空")
    return text


def load_or_create(path: Path) -> Config:
    """Load config.json, creating it from the example on first use."""

    if not path.exists() and EXAMPLE_CONFIG_PATH.is_file():
        path.write_bytes(EXAMPLE_CONFIG_PATH.read_bytes())
    return Config.load(path) if path.exists() else Config()


def load_readonly(path: Path) -> Config:
    """Load without creating files, for status/candidates."""

    for candidate in (path, EXAMPLE_CONFIG_PATH):
        if candidate.is_file():
            return Config.load(candidate)
    return Config()
