"""Configuration loading with compatibility for the existing PowerShell JSON."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_FIELD_ALIASES = {
    "task_name": "TaskName",
    "vpn_interface_alias": "VpnInterfaceAlias",
    "refresh_seconds": "RefreshSeconds",
    "failed_cycle_retry_seconds": "FailedCycleRetrySeconds",
    "disconnected_poll_seconds": "DisconnectedPollSeconds",
    "health_failure_threshold": "HealthFailureThreshold",
    "session_failure_threshold": "SessionFailureThreshold",
    "failed_cycle_backoff_max_seconds": "FailedCycleBackoffMaxSeconds",
    "pause_during_raid": "PauseDuringRaid",
    "game_phase_max_age_days": "GamePhaseMaxAgeDays",
    "game_phase_max_files": "GamePhaseMaxFiles",
    "game_phase_max_bytes_per_file": "GamePhaseMaxBytesPerFile",
    "game_log_roots": "GameLogRoots",
    "target_hosts": "TargetHosts",
    "raid_targets": "RaidTargets",
    "account_name": "AccountName",
    "vpncmd_path": "VpnCmdPath",
    "native_catalog_path": "NativeCatalogPath",
    "native_catalog_max_age_hours": "NativeCatalogMaxAgeHours",
    "nic_name": "NicName",
    "connect_timeout_seconds": "ConnectTimeoutSeconds",
    "disconnect_wait_seconds": "DisconnectWaitSeconds",
    "resource_busy_retry_count": "ResourceBusyRetryCount",
    "failover_timeout_seconds": "FailoverTimeoutSeconds",
    "max_candidates_per_country": "MaxCandidatesPerCountry",
    "max_candidates_total": "MaxCandidatesTotal",
    "tcp_probe_timeout_milliseconds": "TcpProbeTimeoutMilliseconds",
    "discovery_timeout_seconds": "DiscoveryTimeoutSeconds",
    "failure_cooldown_minutes": "FailureCooldownMinutes",
    "cooling_fallback_minutes": "CoolingFallbackMinutes",
    "cooling_fallback_candidates": "CoolingFallbackCandidates",
    "known_good_lifetime_hours": "KnownGoodLifetimeHours",
}


DEFAULT_TARGET_HOSTS = (
    "gw-pvp.escapefromtarkov.ru",
    "gw-pvp.escapefromtarkov.com",
    "gw-pvp-season.escapefromtarkov.ru",
    "gw-pvp-season.escapefromtarkov.com",
    "lobby.escapefromtarkov.ru",
    "lobby.escapefromtarkov.com",
)


def _read_value(data: Mapping[str, Any], name: str, default: Any) -> Any:
    legacy_name = _FIELD_ALIASES[name]
    if name in data:
        return data[name]
    if legacy_name in data:
        return data[legacy_name]
    return default


def _positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    minimum = 0 if allow_zero else 1
    if result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return result


def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{name} must be a boolean")


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a JSON array of strings")
    result = tuple(str(item).strip() for item in value)
    if any(not item for item in result):
        raise ValueError(f"{name} cannot contain an empty string")
    return result


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Typed settings used by both the Python keeper and Windows adapter."""

    task_name: str = "Tarkov-CIS-RouteKeeper"
    vpn_interface_alias: str = "VPN - VPN Client"
    refresh_seconds: int = 30
    failed_cycle_retry_seconds: int = 10
    disconnected_poll_seconds: int = 5
    # Do not rotate a relay because one authorization hostname missed one
    # probe.  These counters are intentionally consecutive-cycle thresholds.
    health_failure_threshold: int = 3
    session_failure_threshold: int = 3
    failed_cycle_backoff_max_seconds: int = 120
    # Login/character selection keep normal health checks. Matching and Raid
    # freeze maintenance until a later menu/PostRaid marker or game exit.
    pause_during_raid: bool = True
    game_phase_max_age_days: int = 2
    game_phase_max_files: int = 24
    game_phase_max_bytes_per_file: int = 256 * 1024
    game_log_roots: tuple[str, ...] = ()
    target_hosts: tuple[str, ...] = DEFAULT_TARGET_HOSTS
    raid_targets: tuple[str, ...] = ()
    account_name: str = "Tarkov-CIS-PlayOnly"
    vpncmd_path: str = r"C:\Program Files\SoftEther VPN Client\vpncmd_x64.exe"
    native_catalog_path: str = ""
    native_catalog_max_age_hours: int = 24
    nic_name: str = "VPN"
    connect_timeout_seconds: int = 18
    disconnect_wait_seconds: int = 15
    resource_busy_retry_count: int = 2
    failover_timeout_seconds: int = 180
    max_candidates_per_country: int = 10
    max_candidates_total: int = 20
    tcp_probe_timeout_milliseconds: int = 1500
    discovery_timeout_seconds: int = 15
    failure_cooldown_minutes: int = 15
    cooling_fallback_minutes: int = 2
    cooling_fallback_candidates: int = 3
    known_good_lifetime_hours: int = 48
    extras: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "task_name",
            "vpn_interface_alias",
            "account_name",
            "vpncmd_path",
            "nic_name",
        ):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} cannot be empty")
            object.__setattr__(self, name, value)
        for name in ("game_log_roots", "target_hosts", "raid_targets"):
            object.__setattr__(self, name, _string_tuple(list(getattr(self, name)), name))
        object.__setattr__(self, "extras", MappingProxyType(dict(self.extras)))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AppConfig":
        if not isinstance(data, Mapping):
            raise ValueError("configuration root must be a JSON object")
        defaults = cls()
        known_names = set(_FIELD_ALIASES) | set(_FIELD_ALIASES.values())
        extras = {key: value for key, value in data.items() if key not in known_names}
        string_fields = (
            "task_name",
            "vpn_interface_alias",
            "account_name",
            "vpncmd_path",
            "native_catalog_path",
            "nic_name",
        )
        list_fields = ("game_log_roots", "target_hosts", "raid_targets")
        integer_fields = (
            "refresh_seconds",
            "failed_cycle_retry_seconds",
            "disconnected_poll_seconds",
            "health_failure_threshold",
            "session_failure_threshold",
            "failed_cycle_backoff_max_seconds",
            "game_phase_max_age_days",
            "game_phase_max_files",
            "game_phase_max_bytes_per_file",
            "connect_timeout_seconds",
            "disconnect_wait_seconds",
            "resource_busy_retry_count",
            "failover_timeout_seconds",
            "max_candidates_per_country",
            "max_candidates_total",
            "tcp_probe_timeout_milliseconds",
            "discovery_timeout_seconds",
            "failure_cooldown_minutes",
            "cooling_fallback_minutes",
            "cooling_fallback_candidates",
            "known_good_lifetime_hours",
            "native_catalog_max_age_hours",
        )
        values: dict[str, Any] = {}
        for name in string_fields:
            values[name] = str(_read_value(data, name, getattr(defaults, name))).strip()
        for name in list_fields:
            values[name] = _string_tuple(_read_value(data, name, getattr(defaults, name)), name)
        for name in integer_fields:
            values[name] = _positive_int(
                _read_value(data, name, getattr(defaults, name)),
                name,
                allow_zero=name in {"cooling_fallback_minutes", "resource_busy_retry_count"},
            )
        values["pause_during_raid"] = _bool(
            _read_value(data, "pause_during_raid", getattr(defaults, "pause_during_raid")),
            "pause_during_raid",
        )
        values["failed_cycle_retry_seconds"] = max(5, values["failed_cycle_retry_seconds"])
        values["disconnected_poll_seconds"] = max(1, values["disconnected_poll_seconds"])
        values["extras"] = extras
        return cls(**values)

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        config_path = Path(path)
        with config_path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return cls.from_mapping(data)

    @classmethod
    def from_json(cls, path: str | Path) -> "AppConfig":
        """Backward-friendly alias for callers that prefer an explicit name."""

        return cls.load(path)

    def to_mapping(self) -> dict[str, Any]:
        result = dict(self.extras)
        for name, legacy_name in _FIELD_ALIASES.items():
            value = getattr(self, name)
            result[legacy_name] = list(value) if isinstance(value, tuple) else value
        return result
