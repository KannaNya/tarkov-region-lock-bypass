from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import FakeNet, relay

from tarkov_cis.models import VpnLease
from tarkov_cis.process import CommandResult
from tarkov_cis.routes import VPN_INTERFACE_METRIC, RouteError, RouteManager
from tarkov_cis.softether import LocalBusyError, SoftEther, SoftEtherError

VPN = 63
LEASE = VpnLease(interface_index=VPN, ipv4="10.211.1.2", gateway="10.211.254.254")


class RouteManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "routes.json"
        self.net = FakeNet()
        self.net.add_default(13, "192.168.68.1")
        self.net.metrics[13] = 35
        self.net.add_default(VPN, LEASE.gateway)

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self) -> RouteManager:
        return RouteManager(self.state, net=self.net)

    def test_sync_pins_vpn_metric_adds_routes_and_records_them(self):
        manager = self.manager()
        manager.sync(["192.0.2.2", "192.0.2.1"], LEASE)
        self.assertIn(("metric", VPN, VPN_INTERFACE_METRIC), self.net.calls)
        self.assertEqual([r.ip for r in manager.owned], ["192.0.2.1", "192.0.2.2"])
        recorded = json.loads(self.state.read_text())["routes"]
        self.assertEqual({item["ip"] for item in recorded}, {"192.0.2.1", "192.0.2.2"})

    def test_sync_replaces_stale_targets_and_routes_of_an_old_lease(self):
        manager = self.manager()
        manager.sync(["192.0.2.1", "192.0.2.2"], LEASE)
        new_lease = VpnLease(VPN, "10.211.9.9", "10.211.0.1")
        self.net.add_default(VPN, new_lease.gateway)
        manager.sync(["192.0.2.2", "192.0.2.3"], new_lease)
        self.assertEqual({(r.ip, r.gateway) for r in manager.owned}, {("192.0.2.2", "10.211.0.1"), ("192.0.2.3", "10.211.0.1")})
        self.assertIn(("delete", "192.0.2.1", VPN), self.net.calls)

    def test_pre_existing_routes_are_never_claimed(self):
        self.net.add_route("192.0.2.1", 32, LEASE.gateway, VPN)
        manager = self.manager()
        manager.sync(["192.0.2.1"], LEASE)
        self.assertEqual(manager.owned, ())
        manager.cleanup()
        self.assertTrue(any(r.destination == "192.0.2.1" for r in self.net.routes))

    def test_crash_recovery_cleans_routes_recorded_by_a_previous_process(self):
        self.manager().sync(["192.0.2.1"], LEASE)
        self.manager().cleanup()
        self.assertFalse(any(r.destination == "192.0.2.1" for r in self.net.routes))
        self.assertFalse(self.state.exists())

    def test_version_1_state_file_is_understood(self):
        self.state.write_text(json.dumps({
            "version": 1,
            "managed_routes": [{"ip": "192.0.2.5", "interface_index": VPN, "gateway": LEASE.gateway}],
            "original_defaults": [],
        }))
        self.assertEqual([r.ip for r in self.manager().owned], ["192.0.2.5"])

    def test_refuses_to_route_when_vpn_would_still_be_the_default_exit(self):
        self.net.metrics[13] = 20000
        with self.assertRaisesRegex(RouteError, "默认出口"):
            self.manager().sync(["192.0.2.1"], LEASE)
        self.assertFalse(any(call[0] == "add" for call in self.net.calls))

    def test_unsafe_or_empty_targets_are_rejected(self):
        with self.assertRaises(ValueError):
            self.manager().sync(["127.0.0.1"], LEASE)
        with self.assertRaises(RouteError):
            self.manager().sync([], LEASE)


class FakeVpncmd:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.results: dict[str, CommandResult] = {}
        self.session = False

    def __call__(self, argv, *, timeout):
        command = next(part for part in argv if part[0].isupper() and not part.startswith("/"))
        self.calls.append(tuple(argv))
        if command == "AccountStatusGet":
            text = "Session Name,SID-VPN-1" if self.session else ""
            return CommandResult(tuple(argv), 0, text, "")
        if command == "AccountList":
            return CommandResult(tuple(argv), 0, "", "")
        return self.results.get(command, CommandResult(tuple(argv), 0, "", ""))

    def commands(self):
        return [next(p for p in call if p[0].isupper() and not p.startswith("/")) for call in self.calls]


class SoftEtherTests(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self.net.aliases["VPN - VPN Client"] = VPN
        self.vpncmd = FakeVpncmd()
        self.vpn = SoftEther(
            vpncmd_path="vpncmd.exe", account_name="Tarkov", interface_alias="VPN - VPN Client",
            nic_name="VPN", runner=self.vpncmd, net=self.net,
        )

    def connect_lease(self):
        self.net.addresses[VPN] = ["10.211.1.2"]
        self.net.add_default(VPN, "10.211.254.254")
        self.vpncmd.session = True

    def test_lease_requires_address_gateway_and_session(self):
        self.assertIsNone(self.vpn.lease())
        self.net.addresses[VPN] = ["169.254.3.4"]
        self.net.add_default(VPN, "10.211.254.254")
        self.assertIsNone(self.vpn.adapter_lease())
        self.net.addresses[VPN] = ["10.211.1.2"]
        self.assertIsNotNone(self.vpn.adapter_lease())
        self.assertIsNone(self.vpn.lease())
        self.vpncmd.session = True
        self.assertEqual(self.vpn.lease(), LEASE)

    def test_connect_creates_account_and_returns_verified_lease(self):
        original_run = self.vpncmd.__call__

        def runner(argv, *, timeout):
            result = original_run(argv, timeout=timeout)
            if "AccountConnect" in argv:
                self.connect_lease()
            return result

        self.vpn._run = runner
        lease = self.vpn.connect(relay("192.0.2.10", 443), timeout=5)
        self.assertEqual(lease, LEASE)
        commands = self.vpncmd.commands()
        self.assertEqual(
            [c for c in commands if c not in ("AccountStatusGet",)],
            ["AccountDisconnect", "AccountList", "AccountCreate", "AccountPasswordSet",
             "AccountRetrySet", "AccountStatusHide", "AccountConnect"],
        )
        create = next(call for call in self.vpncmd.calls if "AccountCreate" in call)
        self.assertIn("/SERVER:192.0.2.10:443", create)

    def test_exit_code_43_is_a_local_busy_error(self):
        self.vpncmd.results["AccountConnect"] = CommandResult((), 43, "", "busy")
        with self.assertRaises(LocalBusyError):
            self.vpn.connect(relay("192.0.2.10"), timeout=5)

    def test_handshake_without_lease_fails_and_disconnects(self):
        with patch("tarkov_cis.softether.time.sleep"), self.assertRaises(SoftEtherError):
            self.vpn.connect(relay("192.0.2.10"), timeout=0.2)
        self.assertEqual(self.vpncmd.commands().count("AccountDisconnect"), 2)

    def test_guard_stops_reconfiguration_between_commands(self):
        calls = []

        def guard():
            calls.append(1)
            if len(calls) > 2:
                raise RuntimeError("match started")

        with self.assertRaisesRegex(RuntimeError, "match started"):
            self.vpn.connect(relay("192.0.2.10"), timeout=5, guard=guard)
        self.assertNotIn("AccountConnect", self.vpncmd.commands())


if __name__ == "__main__":
    unittest.main()
