from __future__ import annotations

from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

import support  # noqa: F401

from tarkov_cis.targets import discover_hosts, is_authorization_host, log_roots, resolve_ipv4


class HostDiscoveryTests(unittest.TestCase):
    def test_only_lobby_gw_and_wsn_hosts_qualify(self):
        self.assertTrue(is_authorization_host("lobby.escapefromtarkov.ru"))
        self.assertTrue(is_authorization_host("wsn-pvp-season-03.escapefromtarkov.com"))
        self.assertTrue(is_authorization_host("gw-pvp-season.escapefromtarkov.com."))
        self.assertFalse(is_authorization_host("cdn.escapefromtarkov.com"))
        self.assertFalse(is_authorization_host("launcher.escapefromtarkov.com"))
        self.assertFalse(is_authorization_host("lobby.escapefromtarkov.ru.evil.example"))

    def test_hosts_come_from_logs_but_ips_in_logs_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "app.log").write_text(
                "https://lobby.escapefromtarkov.ru/login\n"
                "https://wsn-pvp-season-07.escapefromtarkov.com/ws\n"
                "https://cdn.escapefromtarkov.com/file\n"
                "raid server 198.51.100.44:17000\n",
                encoding="utf-8",
            )
            hosts = discover_hosts(["gw-pvp.escapefromtarkov.ru", "example.com"], [Path(directory)])
        self.assertEqual(
            hosts,
            ("gw-pvp.escapefromtarkov.ru", "lobby.escapefromtarkov.ru", "wsn-pvp-season-07.escapefromtarkov.com"),
        )

    def test_configured_root_seeds_the_same_path_on_other_drives(self):
        with tempfile.TemporaryDirectory() as directory:
            drive = Path(directory)
            (drive / "Games" / "Tarkov" / "Logs").mkdir(parents=True)
            (drive / "GAME" / "Tarkov" / "Logs").mkdir(parents=True)
            roots = log_roots([r"Z:\Games\Tarkov\Logs"], drives=[drive])
        self.assertIn(drive / "Games" / "Tarkov" / "Logs", roots)
        self.assertIn(drive / "GAME" / "Tarkov" / "Logs", roots)


class ResolveTests(unittest.TestCase):
    def test_all_hosts_resolve_within_one_timeout_and_failures_are_skipped(self):
        release = threading.Event()

        def resolver(host, *_args):
            if host == "slow":
                release.wait(5)
                return []
            if host == "broken":
                raise socket.gaierror("no such host")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.9" if host == "b" else "192.0.2.10", 443))]

        started = time.monotonic()
        ips = resolve_ipv4(["a", "b", "broken", "slow"], timeout=0.5, resolver=resolver)
        release.set()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(ips, ("192.0.2.9", "192.0.2.10"))


if __name__ == "__main__":
    unittest.main()
