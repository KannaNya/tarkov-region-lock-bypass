from __future__ import annotations

import base64
import csv
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from tarkov_cis.catalog import (
    extract_tcp_ports_from_openvpn_config,
    parse_vpngate_csv,
    rc4_transform,
    read_vpngate_native_catalog,
)
from tarkov_cis.config import AppConfig
from tarkov_cis.models import ConnectionPhase, FailureRecord, Relay
from tarkov_cis.relay_selector import (
    next_cooling_fallback_at,
    plan_failover_cycle,
    select_relay_candidates,
)
from tarkov_cis.state_machine import ConnectionEvent, ConnectionStateMachine, InvalidTransition


UTC = timezone.utc


def encoded(profile: str) -> str:
    return base64.b64encode(profile.encode("utf-8")).decode("ascii")


def relay(ip: str, port: int, country: str = "RU", **values: object) -> Relay:
    defaults: dict[str, object] = {
        "host_name": f"vpn-{ip.replace('.', '-')}.opengw.net",
        "ip": ip,
        "port": port,
        "country_short": country,
        "score": 900,
        "ping": 30,
        "sessions": 2,
        "source": "NativeCatalog",
        "source_priority": 3,
    }
    defaults.update(values)
    return Relay(**defaults)  # type: ignore[arg-type]


class ConfigTests(unittest.TestCase):
    def test_existing_example_and_unknown_fields_are_preserved(self) -> None:
        config = AppConfig.load(ROOT / "config.example.json")
        self.assertEqual(config.task_name, "Tarkov-CIS-RouteKeeper")
        self.assertEqual(config.refresh_seconds, 30)
        self.assertTrue(config.disconnect_at_menu)
        self.assertEqual(config.failed_cycle_backoff_max_seconds, 10)
        self.assertEqual(config.cooling_fallback_minutes, 0)
        self.assertIn("gw-pvp.escapefromtarkov.ru", config.target_hosts)

        extended = AppConfig.from_mapping({"TaskName": "Test", "RefreshSeconds": 7, "FutureOption": {"x": 1}})
        self.assertEqual(extended.task_name, "Test")
        self.assertEqual(extended.refresh_seconds, 7)
        self.assertEqual(extended.extras["FutureOption"], {"x": 1})
        self.assertEqual(extended.to_mapping()["FutureOption"], {"x": 1})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"TaskName":"Loaded","RaidTargets":[]}', encoding="utf-8")
            self.assertEqual(AppConfig.from_json(path).task_name, "Loaded")


class CatalogTests(unittest.TestCase):
    HEADER = [
        "#HostName", "IP", "Score", "Ping", "Speed", "CountryLong", "CountryShort",
        "NumVpnSessions", "Uptime", "TotalUsers", "TotalTraffic", "LogType", "Operator",
        "Message", "OpenVPN_ConfigData_Base64",
    ]

    def _csv(self, rows: list[list[str]]) -> str:
        output = StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["*vpn_servers"])
        writer.writerow(self.HEADER)
        writer.writerows(rows)
        writer.writerow(["*"])
        return output.getvalue()

    def _row(self, host: str, ip: str, country: str, profile: str, score: int = 100) -> list[str]:
        return [host, ip, str(score), "20", "50000000", country, country, "2", "1", "1", "1", "", "", "message,with,commas", encoded(profile)]

    def test_only_unambiguous_tcp_profiles_are_kept_and_deduplicated(self) -> None:
        rows = [
            self._row("ru-low.opengw.net", "192.0.2.10", "RU", "client\nproto tcp\nremote 192.0.2.10 443\n", 100),
            self._row("ru-best.opengw.net", "192.0.2.10", "RU", "client\nproto tcp-client\nremote 192.0.2.10 443\n", 900),
            self._row("ua-udp.opengw.net", "192.0.2.20", "UA", "client\nproto udp\nremote 192.0.2.20 1194\n", 2000),
            self._row("kz-explicit.opengw.net", "192.0.2.30", "KZ", "client\nproto udp\nremote 192.0.2.30 992 tcp\n", 800),
            self._row("by-ambiguous.opengw.net", "192.0.2.40", "BY", "client\nremote 192.0.2.40 5555\n", 700),
            self._row("jp.opengw.net", "192.0.2.50", "JP", "client\nproto tcp\nremote 192.0.2.50 443\n", 5000),
        ]
        parsed = parse_vpngate_csv(self._csv(rows))
        self.assertEqual([item.endpoint for item in parsed], ["192.0.2.10:443", "192.0.2.30:992"])
        self.assertEqual(parsed[0].host_name, "ru-best.opengw.net")
        self.assertEqual(parsed[0].speed_mbps, 50.0)

    def test_connection_blocks_inherit_or_override_tcp(self) -> None:
        profile = """client
proto tcp-client
<connection>
remote 192.0.2.1 443
</connection>
<connection>
proto udp
remote 192.0.2.1 1194
remote 192.0.2.1 992 tcp
</connection>
"""
        self.assertEqual(extract_tcp_ports_from_openvpn_config(encoded(profile)), (443, 992))

    @staticmethod
    def _pack(elements: list[tuple[str, int, list[object]]]) -> bytes:
        payload = bytearray(struct.pack(">I", len(elements)))
        for name, element_type, values in elements:
            name_bytes = name.encode("ascii")
            payload.extend(struct.pack(">I", len(name_bytes) + 1))
            payload.extend(name_bytes)
            payload.extend(struct.pack(">II", element_type, len(values)))
            for value in values:
                if element_type == 0:
                    payload.extend(struct.pack(">I", int(value)))
                elif element_type == 1:
                    data = bytes(value)
                    payload.extend(struct.pack(">I", len(data)))
                    payload.extend(data)
                elif element_type in {2, 3}:
                    data = str(value).encode("utf-8") + (b"\0" if element_type == 3 else b"")
                    payload.extend(struct.pack(">I", len(data)))
                    payload.extend(data)
                elif element_type == 4:
                    payload.extend(struct.pack(">Q", int(value)))
                else:
                    raise AssertionError("unsupported synthetic PACK type")
        return bytes(payload)

    def _native_fixture(self, timestamp: datetime, *, compressed: bool) -> bytes:
        inner = self._pack(
            [
                ("CountryShort", 2, ["RU", "JP"]),
                ("CountryFull", 2, ["Russian Federation", "Japan"]),
                ("Fqdn", 2, ["vpn-test-ru.opengw.net", "vpn-test-jp.opengw.net"]),
                ("IP", 2, ["192.0.2.10", "192.0.2.20"]),
                ("SslPorts", 2, ["443 992 invalid 70000", "5555"]),
                ("UdpPort", 0, [2061, 0]),
                ("PingToJapan", 0, [88, 12]),
                ("SpeedToJapan", 4, [52_000_000, 100_000_000]),
                ("Score", 4, [900_001, 900_002]),
                ("NumClients", 0, [3, 4]),
            ]
        )
        server_payload = inner
        if compressed:
            server_payload = struct.pack(">I", len(inner)) + zlib.compress(inner)
        outer = self._pack(
            [
                ("data", 1, [server_payload]),
                ("data_size", 0, [len(server_payload)]),
                ("sign", 1, [bytes(128)]),
                ("compressed", 0, [1 if compressed else 0]),
            ]
        )
        header = bytearray(0xF0)
        stamp = timestamp.strftime("%Y%m%d_%H%M%S.%f")[:-3]
        header_text = f"[VPNGate Data File]\r\n{stamp}\r\n\r\n".encode("ascii")
        header[: len(header_text)] = header_text
        seed = bytes(range(1, 21))
        encrypted = rc4_transform(outer, hashlib.sha1(seed).digest())
        return bytes(header) + seed + encrypted

    def test_native_plugin_catalog_expands_ssl_ports_and_optional_zlib(self) -> None:
        now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        for compressed in (False, True):
            with self.subTest(compressed=compressed), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "VPNGate.dat"
                path.write_bytes(self._native_fixture(now - timedelta(minutes=5), compressed=compressed))
                catalog = read_vpngate_native_catalog(path, max_age_hours=1, now=now)
                self.assertEqual(catalog.source_row_count, 2)
                self.assertEqual(catalog.compressed, compressed)
                self.assertTrue(catalog.signature_marker_present)
                self.assertEqual(
                    [item.endpoint for item in catalog.relays],
                    ["192.0.2.10:443", "192.0.2.10:992", "udp://192.0.2.10:2061"],
                )
                self.assertEqual(catalog.relays[0].source, "NativeCatalog")


class RelaySelectorTests(unittest.TestCase):
    def test_stable_identity_country_round_robin_and_alternate_port_fallback(self) -> None:
        relays = [
            relay("192.0.2.10", 443),
            relay("192.0.2.10", 992, score=899),
            relay("192.0.2.11", 443, score=800),
            relay("192.0.2.12", 443, country="UA", score=1200),
            relay("192.0.2.13", 443, country="KZ", score=700),
        ]
        selection = select_relay_candidates(relays, per_country_limit=5, total_limit=5)
        self.assertEqual(
            [item.country_short for item in selection.candidates[:4]],
            ["RU", "UA", "KZ", "RU"],
        )
        self.assertEqual(selection.candidates[-1].endpoint, "192.0.2.10:992")
        self.assertEqual(relays[0].identity, relays[1].identity)

    def test_specific_endpoint_cools_without_hiding_alternate_port(self) -> None:
        now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        relays = [relay("192.0.2.10", 443), relay("192.0.2.10", 992)]
        failures = [FailureRecord("192.0.2.10:443", now, "probe failed")]
        selection = select_relay_candidates(relays, failures, now=now)
        self.assertEqual([item.endpoint for item in selection.candidates], ["192.0.2.10:992"])
        self.assertIn("192.0.2.10:443", selection.cooling_until)

    def test_all_cooling_uses_bounded_two_minute_fallback(self) -> None:
        now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        relays = [
            relay("192.0.2.10", 443),
            relay("192.0.2.10", 992),
            relay("192.0.2.11", 443),
            relay("192.0.2.12", 443),
        ]
        old = now - timedelta(minutes=3)
        failures = [FailureRecord(item.endpoint, old, "offline") for item in relays]
        plan = plan_failover_cycle(relays, failures, now=now, total_limit=10, fallback_limit=3)
        self.assertTrue(plan.used_cooling_fallback)
        self.assertEqual([item.ip for item in plan.candidates], ["192.0.2.10", "192.0.2.11", "192.0.2.12"])

        recent = [FailureRecord(item.endpoint, now - timedelta(seconds=30), "offline") for item in relays]
        waiting = plan_failover_cycle(relays, recent, now=now, total_limit=10, fallback_limit=3)
        self.assertFalse(waiting.candidates)
        self.assertEqual(waiting.retry_at, now + timedelta(minutes=1, seconds=30))

        stale = FailureRecord("198.51.100.200:443", now - timedelta(minutes=20), "stale")
        self.assertEqual(
            next_cooling_fallback_at(relays, recent + [stale]),
            now + timedelta(minutes=1, seconds=30),
        )


class StateMachineTests(unittest.TestCase):
    def test_happy_path_and_disconnect(self) -> None:
        machine = ConnectionStateMachine.initial()
        candidate = relay("192.0.2.10", 443)
        self.assertEqual(machine.start(), ConnectionPhase.DISCOVERING)
        self.assertEqual(machine.candidate_selected(candidate), ConnectionPhase.CONNECTING)
        self.assertEqual(machine.tcp_connected(), ConnectionPhase.VERIFYING_SESSION)
        self.assertEqual(machine.session_established(), ConnectionPhase.APPLYING_ROUTES)
        self.assertEqual(machine.routes_applied(), ConnectionPhase.READY)
        self.assertEqual(machine.disconnect(), ConnectionPhase.DISCONNECTING)
        self.assertEqual(machine.disconnected(), ConnectionPhase.DISCONNECTED)
        self.assertEqual(len(machine.history), 7)

    def test_failure_retry_and_invalid_transition_are_explicit(self) -> None:
        machine = ConnectionStateMachine.initial()
        machine.start()
        machine.candidate_selected(relay("192.0.2.10", 443))
        self.assertEqual(machine.attempt_failed("TCP refused"), ConnectionPhase.DISCOVERING)
        self.assertEqual(machine.failure_reason, "TCP refused")
        self.assertEqual(machine.no_candidates(), ConnectionPhase.COOLING)
        self.assertEqual(machine.retry_ready(), ConnectionPhase.DISCOVERING)
        with self.assertRaises(InvalidTransition):
            machine.transition(ConnectionEvent.ROUTES_APPLIED)

    def test_existing_session_can_be_restored_from_keeper_entry_states(self) -> None:
        builders = {
            ConnectionPhase.DISCONNECTED: lambda machine: None,
            ConnectionPhase.DISCOVERING: lambda machine: machine.start(),
            ConnectionPhase.COOLING: lambda machine: (machine.start(), machine.no_candidates()),
            ConnectionPhase.FAILED: lambda machine: (
                machine.start(),
                machine.transition(ConnectionEvent.DISCOVERY_FAILED, reason="catalog unavailable"),
            ),
        }
        for expected_start, prepare in builders.items():
            with self.subTest(start=expected_start.value):
                machine = ConnectionStateMachine.initial()
                prepare(machine)
                self.assertEqual(machine.phase, expected_start)
                self.assertEqual(machine.session_restored(), ConnectionPhase.APPLYING_ROUTES)
                self.assertEqual(machine.routes_applied(), ConnectionPhase.READY)
                self.assertIs(machine.history[-2].event, ConnectionEvent.SESSION_RESTORED)
                self.assertIs(machine.history[-1].event, ConnectionEvent.ROUTES_APPLIED)

    def test_route_failure_is_terminal_and_retains_endpoint_evidence(self) -> None:
        for fail_while_ready in (False, True):
            with self.subTest(fail_while_ready=fail_while_ready):
                machine = ConnectionStateMachine.initial()
                candidate = relay("192.0.2.10", 443)
                machine.start()
                machine.candidate_selected(candidate)
                machine.tcp_connected()
                machine.session_established()
                if fail_while_ready:
                    machine.routes_applied()
                phase = machine.transition(
                    ConnectionEvent.ROUTES_FAILED,
                    reason="physical default route missing",
                )
                self.assertEqual(phase, ConnectionPhase.FAILED)
                self.assertEqual(machine.failure_reason, "physical default route missing")
                self.assertIsNone(machine.candidate)
                self.assertEqual(machine.history[-1].endpoint, candidate.endpoint)
                self.assertEqual(machine.reset(), ConnectionPhase.DISCONNECTED)
                self.assertEqual(machine.failure_reason, "")

    def test_cooling_retry_time_drives_a_bounded_fallback_attempt(self) -> None:
        failed_at = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        candidate = relay("192.0.2.10", 443)
        failures = [FailureRecord(candidate.endpoint, failed_at, "offline")]
        before_retry = failed_at + timedelta(seconds=30)
        waiting = plan_failover_cycle([candidate], failures, now=before_retry)
        self.assertFalse(waiting.candidates)
        self.assertEqual(waiting.retry_at, failed_at + timedelta(minutes=2))

        machine = ConnectionStateMachine.initial()
        machine.start()
        self.assertEqual(machine.no_candidates(), ConnectionPhase.COOLING)
        with self.assertRaises(InvalidTransition):
            machine.candidate_selected(candidate)

        retry_time = waiting.retry_at
        self.assertIsNotNone(retry_time)
        self.assertEqual(machine.retry_ready(), ConnectionPhase.DISCOVERING)
        retry_plan = plan_failover_cycle([candidate], failures, now=retry_time)
        self.assertTrue(retry_plan.used_cooling_fallback)
        self.assertEqual(retry_plan.candidates, (candidate,))
        self.assertEqual(
            machine.candidate_selected(retry_plan.candidates[0]),
            ConnectionPhase.CONNECTING,
        )


if __name__ == "__main__":
    unittest.main()
