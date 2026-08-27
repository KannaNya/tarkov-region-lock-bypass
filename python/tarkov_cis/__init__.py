"""Standard-library core for Tarkov CIS VPN Gate split routing."""

from .catalog import (
    CIS_COUNTRY_PRIORITY,
    NativeCatalog,
    NativeCatalogReader,
    extract_tcp_ports_from_openvpn_config,
    parse_vpngate_csv,
    read_vpngate_native_catalog,
)
from .config import AppConfig
from .models import ConnectionPhase, FailureRecord, Relay, SelectionResult
from .relay_selector import (
    country_round_robin,
    merge_recent_known_good,
    next_cooling_fallback_at,
    plan_failover_cycle,
    select_cooling_fallback,
    select_relay_candidates,
)
from .state_machine import ConnectionEvent, ConnectionStateMachine, InvalidTransition, Transition

__all__ = [
    "AppConfig",
    "CIS_COUNTRY_PRIORITY",
    "ConnectionEvent",
    "ConnectionPhase",
    "ConnectionStateMachine",
    "FailureRecord",
    "InvalidTransition",
    "NativeCatalog",
    "NativeCatalogReader",
    "Relay",
    "SelectionResult",
    "Transition",
    "country_round_robin",
    "extract_tcp_ports_from_openvpn_config",
    "merge_recent_known_good",
    "next_cooling_fallback_at",
    "parse_vpngate_csv",
    "plan_failover_cycle",
    "read_vpngate_native_catalog",
    "select_cooling_fallback",
    "select_relay_candidates",
]
