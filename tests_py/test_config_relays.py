from __future__ import annotations

import unittest

from support import ROOT, relay

from tarkov_cis.config import Config
from tarkov_cis.relays import FailureCooldown, order_candidates


class ConfigTests(unittest.TestCase):
    def test_example_config_loads(self):
        config = Config.load(ROOT / "config.example.json")
        self.assertEqual(config.vpn_interface_alias, "VPN - VPN Client")
        self.assertTrue(config.pause_during_raid)
        self.assertIn("wsn-pvp-season-01.escapefromtarkov.com", config.target_hosts)

    def test_obsolete_keys_are_ignored_and_pascal_or_snake_case_accepted(self):
        config = Config.from_mapping({
            "HealthFailureThreshold": 3,
            "DisconnectAtRaid": False,
            "KnownGoodLifetimeHours": 48,
            "RefreshSeconds": 45,
            "session_failure_threshold": 5,
            "VpnCmdPath": r"D:\SoftEther\vpncmd.exe",
        })
        self.assertEqual(config.refresh_seconds, 45)
        self.assertEqual(config.session_failure_threshold, 5)
        self.assertEqual(str(config.native_catalog), r"D:\SoftEther\VPNGate.dat")

    def test_invalid_values_are_reported_by_their_config_key(self):
        for data, key in (
            ({"RefreshSeconds": 0}, "RefreshSeconds"),
            ({"RefreshSeconds": True}, "RefreshSeconds"),
            ({"PauseDuringRaid": "maybe"}, "PauseDuringRaid"),
            ({"TargetHosts": "lobby.escapefromtarkov.ru"}, "TargetHosts"),
            ({"TaskName": " "}, "TaskName"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                Config.from_mapping(data)


class OrderCandidatesTests(unittest.TestCase):
    def test_countries_round_robin_with_ru_first(self):
        relays = [
            relay("192.0.2.1", country="RU", speed_mbps=10),
            relay("192.0.2.2", country="RU", speed_mbps=90),
            relay("192.0.2.3", country="UA", speed_mbps=50),
            relay("192.0.2.4", country="KZ", speed_mbps=50),
        ]
        ordered = order_candidates(relays, per_country=10, total=10)
        self.assertEqual([r.ip for r in ordered], ["192.0.2.2", "192.0.2.3", "192.0.2.4", "192.0.2.1"])

    def test_distinct_ips_come_before_extra_ports_of_the_same_relay(self):
        relays = [
            relay("192.0.2.1", 443, speed_mbps=90),
            relay("192.0.2.1", 992, speed_mbps=90),
            relay("192.0.2.2", 443, speed_mbps=10),
        ]
        ordered = order_candidates(relays, per_country=10, total=10)
        self.assertEqual([r.endpoint for r in ordered], ["192.0.2.1:443", "192.0.2.2:443", "192.0.2.1:992"])

    def test_limits_and_duplicate_endpoints(self):
        relays = [relay("192.0.2.1", source="HttpsApi"), relay("192.0.2.1", source="NativeCatalog")]
        relays += [relay(f"192.0.2.{i}") for i in range(10, 20)]
        ordered = order_candidates(relays, per_country=3, total=2)
        self.assertEqual(len(ordered), 2)
        self.assertEqual(ordered[0].source, "NativeCatalog")


class CooldownTests(unittest.TestCase):
    def test_failed_endpoints_cool_down_then_come_back(self):
        now = [0.0]
        cooldown = FailureCooldown(60, clock=lambda: now[0])
        a, b = relay("192.0.2.1"), relay("192.0.2.2")
        cooldown.record_failure(a)
        self.assertEqual(cooldown.filter([a, b]), [b])
        now[0] = 61
        self.assertEqual(cooldown.filter([a, b]), [a, b])

    def test_when_everything_cools_the_oldest_failure_is_retried_first(self):
        now = [0.0]
        cooldown = FailureCooldown(600, clock=lambda: now[0])
        a, b = relay("192.0.2.1"), relay("192.0.2.2")
        cooldown.record_failure(b)
        now[0] = 5
        cooldown.record_failure(a)
        self.assertEqual(cooldown.filter([a, b]), [b, a])
        cooldown.record_success(a)
        self.assertEqual(cooldown.filter([a, b]), [a])


if __name__ == "__main__":
    unittest.main()
