from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from tarkov_cis.eft_logs import (
    AuthorizationTarget,
    discover_authorization_hosts,
    resolve_authorization_targets,
)
from tarkov_cis.keeper import KeeperPhase, KeeperService
from tarkov_cis.models import ConnectionPhase
from tarkov_cis.process_runner import CommandResult, CommandTimeout, run_command
from tarkov_cis.routing import RouteError, RouteManager
from tarkov_cis.softether import (
    LocalResourceBusyError,
    SoftEtherClient,
    SoftEtherError,
    VpnLease,
)


def result(argv=(), code=0, stdout="", stderr="") -> CommandResult:
    return CommandResult(tuple(str(value) for value in argv), code, stdout, stderr)


class ProcessRunnerTests(unittest.TestCase):
    @patch("tarkov_cis.process_runner.subprocess.run")
    def test_runner_never_uses_shell_and_always_passes_timeout(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(
            ["tool.exe"], 7, stdout=b"out", stderr=b"err"
        )
        observed = run_command(["tool.exe", "arg"], timeout=3)
        self.assertEqual(7, observed.exit_code)
        self.assertEqual("out", observed.stdout)
        self.assertEqual("err", observed.stderr)
        kwargs = mocked_run.call_args.kwargs
        self.assertIs(False, kwargs["shell"])
        self.assertEqual(3, kwargs["timeout"])
        self.assertIs(subprocess.PIPE, kwargs["stdout"])
        self.assertIs(subprocess.PIPE, kwargs["stderr"])

    @patch("tarkov_cis.process_runner.subprocess.run")
    def test_timeout_preserves_partial_output(self, mocked_run):
        mocked_run.side_effect = subprocess.TimeoutExpired(
            ["tool.exe"], 2, output=b"partial", stderr=b"late"
        )
        with self.assertRaises(CommandTimeout) as raised:
            run_command(["tool.exe"], timeout=2)
        self.assertTrue(raised.exception.result.timed_out)
        self.assertEqual("partial", raised.exception.result.stdout)


class SoftEtherTests(unittest.TestCase):
    def test_verified_connection_requires_sid_and_non_apipa_lease(self):
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            if "AccountStatusGet" in argv:
                return result(argv, stdout="Session Name|SID-ABC-123")
            return result(
                argv,
                stdout=(
                    '{"InterfaceIndex":12,"InterfaceAlias":"VPN - VPN Client",'
                    '"IPv4":"10.211.1.7","Gateway":"10.211.1.1"}'
                ),
            )

        client = SoftEtherClient(runner=runner)
        lease = client.verified_connection()
        self.assertIsNotNone(lease)
        self.assertEqual(12, lease.interface_index)
        self.assertIn("AccountStatusGet", calls[0])

    def test_apipa_adapter_is_not_a_successful_connection(self):
        def runner(argv, **_kwargs):
            if "AccountStatusGet" in argv:
                return result(argv, stdout="SID-VALID")
            return result(
                argv,
                stdout=(
                    '{"InterfaceIndex":12,"InterfaceAlias":"VPN",'
                    '"IPv4":"169.254.10.2","Gateway":"169.254.10.1"}'
                ),
            )

        self.assertIsNone(SoftEtherClient(runner=runner).verified_connection())

    def test_account_configuration_disables_internal_infinite_retry(self):
        calls = []

        def runner(argv, **_kwargs):
            calls.append(tuple(argv))
            if "AccountList" in argv:
                return result(argv, stdout="Tarkov-CIS-PlayOnly")
            return result(argv)

        relay = SimpleNamespace(ip="31.43.129.131", host_name="vpn.example", port=5555)
        SoftEtherClient(runner=runner).ensure_account(relay)
        flat = "\n".join(" ".join(call) for call in calls)
        self.assertIn("AccountSet", flat)
        self.assertIn("/SERVER:31.43.129.131:5555", flat)
        self.assertIn("AccountRetrySet", flat)
        self.assertIn("/NUM:0", flat)

    def test_exit_code_43_is_classified_as_local_resource_busy(self):
        def runner(argv, **_kwargs):
            return result(argv, code=43, stderr="local resource busy")

        client = SoftEtherClient(runner=runner)
        with self.assertRaises(LocalResourceBusyError):
            client.ensure_account(
                SimpleNamespace(ip="31.43.129.131", host_name="vpn.example", port=5555)
            )

    def test_disconnect_exit_43_with_stuck_session_is_local_not_remote(self):
        def runner(argv, **_kwargs):
            if "AccountDisconnect" in argv:
                return result(argv, code=43, stderr="local resource busy")
            if "AccountStatusGet" in argv:
                return result(argv, stdout="Session Name|SID-STILL-BUSY")
            return result(
                argv,
                stdout=(
                    '{"InterfaceIndex":12,"InterfaceAlias":"VPN",'
                    '"IPv4":"10.1.0.2","Gateway":"10.1.0.1"}'
                ),
            )

        with self.assertRaises(LocalResourceBusyError):
            SoftEtherClient(runner=runner).disconnect(timeout=0.01, poll_interval=0.001)


class RouteManagerTests(unittest.TestCase):
    def test_only_active_store_host_routes_are_created_and_owned(self):
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            script = argv[-1]
            if "$ranked=@(Get-NetRoute" in script:
                return result(
                    argv,
                    stdout=(
                        '{"InterfaceIndex":7,"NextHop":"192.168.31.1",'
                        '"EffectiveMetric":25}'
                    ),
                )
            if "Select-Object InterfaceIndex,NextHop,RouteMetric" in script:
                return result(
                    argv,
                    stdout=(
                        '{"InterfaceIndex":12,"NextHop":"10.1.0.1",'
                        '"RouteMetric":0}'
                    ),
                )
            if script.startswith("$ErrorActionPreference='SilentlyContinue'"):
                return result(argv, code=1)
            return result(argv)

        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary, "routes.json")
            manager = RouteManager(runner=runner, state_path=state_path)
            lease = VpnLease(12, "VPN - VPN Client", "10.1.0.2", "10.1.0.1")
            manager.sync(["203.0.113.10"], lease)
            self.assertEqual(1, len(manager.managed))
            self.assertTrue(state_path.is_file())
            # A separate stop process can load the exact owned route and the
            # original VPN default metric from disk.
            restored = RouteManager(runner=runner, state_path=state_path)
            restored.cleanup()
            self.assertFalse(state_path.exists())
        scripts = "\n".join(call[-1] for call in calls)
        self.assertIn("203.0.113.10/32", scripts)
        self.assertIn("PolicyStore ActiveStore", scripts)
        self.assertIn("Set-NetRoute -PolicyStore ActiveStore -RouteMetric 9000", scripts)
        self.assertIn("Set-NetRoute -PolicyStore ActiveStore -RouteMetric 0", scripts)
        self.assertNotIn("PersistentStore", scripts)
        self.assertNotIn("Set-Dns", scripts)
        self.assertNotIn("Firewall", scripts)

    def test_refuses_to_continue_if_vpn_still_wins_default_route(self):
        def runner(argv, **_kwargs):
            script = argv[-1]
            if "$ranked=@(Get-NetRoute" in script:
                return result(argv, stdout='{"InterfaceIndex":12}')
            if "Select-Object InterfaceIndex,NextHop,RouteMetric" in script:
                return result(
                    argv,
                    stdout=(
                        '{"InterfaceIndex":12,"NextHop":"10.1.0.1",'
                        '"RouteMetric":0}'
                    ),
                )
            return result(argv, code=1 if "SilentlyContinue" in script else 0)

        with tempfile.TemporaryDirectory() as temporary:
            manager = RouteManager(
                runner=runner, state_path=Path(temporary, "routes.json")
            )
            lease = VpnLease(12, "VPN", "10.1.0.2", "10.1.0.1")
            with self.assertRaises(RouteError):
                manager.sync(["203.0.113.10"], lease)

    def test_unsafe_or_non_ipv4_targets_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = RouteManager(
                runner=lambda argv, **kwargs: result(argv),
                state_path=Path(temporary, "routes.json"),
            )
            lease = VpnLease(12, "VPN", "10.1.0.2", "10.1.0.1")
            for target in ("0.0.0.0", "127.0.0.1", "169.254.1.1", "::1"):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    manager.add_target(target, lease)


class EftLogTests(unittest.TestCase):
    def test_only_authorization_families_are_discovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary, "application.log")
            log.write_text(
                "https://lobby.escapefromtarkov.ru/login\n"
                "wss://wsn-pvp-season-01.escapefromtarkov.com/ws\n"
                "https://gw-pvp.escapefromtarkov.com/session\n"
                "https://cdn.escapefromtarkov.com/file\n"
                "Raid server 198.51.100.44:17000\n",
                encoding="utf-8",
            )
            hosts = discover_authorization_hosts([temporary], static_hosts=[])
        self.assertEqual(
            (
                "gw-pvp.escapefromtarkov.com",
                "lobby.escapefromtarkov.ru",
                "wsn-pvp-season-01.escapefromtarkov.com",
            ),
            hosts,
        )
        self.assertNotIn("198.51.100.44", hosts)

    def test_resolution_groups_shared_ip_without_accepting_other_domains(self):
        def resolver(host, *_args):
            return [(None, None, None, None, ("203.0.113.10", 443))]

        targets = resolve_authorization_targets(
            [
                "lobby.escapefromtarkov.ru",
                "wsn-pvp-season-01.escapefromtarkov.com",
                "cdn.escapefromtarkov.com",
            ],
            resolver=resolver,
        )
        self.assertEqual(1, len(targets))
        self.assertEqual(2, len(targets[0].hostnames))

    def test_dns_resolution_has_one_bounded_total_timeout(self):
        def stalled_resolver(*_args):
            time.sleep(0.5)
            return []

        started = time.monotonic()
        targets = resolve_authorization_targets(
            [
                "lobby.escapefromtarkov.ru",
                "gw-pvp.escapefromtarkov.com",
            ],
            resolver=stalled_resolver,
            timeout=0.02,
        )
        self.assertEqual((), targets)
        self.assertLess(time.monotonic() - started, 0.2)


class KeeperTests(unittest.TestCase):
    def test_failed_relay_switches_to_next_candidate(self):
        first = SimpleNamespace(
            endpoint="1.1.1.1:443", ip="1.1.1.1", port=443, country_short="RU"
        )
        second = SimpleNamespace(
            endpoint="2.2.2.2:443", ip="2.2.2.2", port=443, country_short="UA"
        )

        class Provider:
            def __init__(self):
                self.failures = []

            def candidates(self):
                return [first, second]

            def record_failure(self, relay, reason):
                self.failures.append((relay, reason))

        class FakeSoftEther:
            def __init__(self):
                self.attempts = []

            def verified_connection(self):
                return None

            def probe_tcp(self, _relay, **_kwargs):
                return True

            def connect(self, relay, **_kwargs):
                self.attempts.append(relay)
                if relay is first:
                    # vpncmd timeouts are local command errors, but they still
                    # belong to this relay attempt and must not abort the
                    # whole candidate batch.
                    raise CommandTimeout("vpncmd timed out", result())
                return VpnLease(12, "VPN", "10.1.0.2", "10.1.0.1")

            def disconnect(self, **_kwargs):
                return True

        class FakeRoutes:
            def __init__(self):
                self.synced = []

            def cleanup(self):
                pass

            def sync(self, ips, _lease, **_kwargs):
                self.synced.append(tuple(ips))

        provider = Provider()
        softether = FakeSoftEther()
        routes = FakeRoutes()
        logs = []
        config = SimpleNamespace(
            game_log_roots=(),
            target_hosts=("lobby.escapefromtarkov.ru",),
            refresh_seconds=30,
            failed_cycle_retry_seconds=10,
            failover_timeout_seconds=180,
            connect_timeout_seconds=18,
            tcp_probe_timeout_milliseconds=1500,
        )
        keeper = KeeperService(
            config=config,
            candidate_provider=provider,
            softether=softether,
            routes=routes,
            logger=logs.append,
        )
        with patch(
            "tarkov_cis.keeper.resolve_authorization_targets",
            return_value=(AuthorizationTarget("203.0.113.10", ("lobby.escapefromtarkov.ru",)),),
        ):
            self.assertTrue(keeper.run_cycle())

        self.assertEqual([first, second], softether.attempts)
        self.assertEqual(1, len(provider.failures))
        self.assertEqual(KeeperPhase.READY, keeper.status.phase)
        self.assertEqual(ConnectionPhase.READY, keeper.status.connection_phase)
        self.assertEqual(ConnectionPhase.READY, keeper.machine.phase)
        self.assertEqual("UA", keeper.status.country)
        self.assertTrue(any("[switching]" in line for line in logs))
        self.assertEqual([("203.0.113.10",)], routes.synced)

    def test_local_busy_does_not_cool_remote_relay(self):
        relay = SimpleNamespace(
            endpoint="1.1.1.1:443", ip="1.1.1.1", port=443, country_short="RU"
        )

        class Provider:
            def __init__(self):
                self.failures = []

            def candidates(self):
                return [relay]

            def record_failure(self, item, reason):
                self.failures.append((item, reason))

        class BusySoftEther:
            def __init__(self):
                self.attempts = 0

            def verified_connection(self):
                return None

            def probe_tcp(self, _relay, **_kwargs):
                return True

            def connect(self, _relay, **_kwargs):
                self.attempts += 1
                raise LocalResourceBusyError("exit code 43")

            def disconnect(self, **_kwargs):
                return True

        class Routes:
            def cleanup(self):
                pass

        provider = Provider()
        softether = BusySoftEther()
        keeper = KeeperService(
            config=SimpleNamespace(
                game_log_roots=(),
                target_hosts=("lobby.escapefromtarkov.ru",),
                refresh_seconds=30,
                failed_cycle_retry_seconds=10,
                failover_timeout_seconds=180,
                connect_timeout_seconds=18,
                tcp_probe_timeout_milliseconds=1500,
            ),
            candidate_provider=provider,
            softether=softether,
            routes=Routes(),
        )
        with patch.object(keeper._stop_event, "wait", return_value=False):
            self.assertFalse(keeper.run_cycle())
        self.assertEqual(3, softether.attempts)
        self.assertEqual([], provider.failures)
        self.assertEqual(KeeperPhase.RETRY_WAIT, keeper.status.phase)
        self.assertEqual(ConnectionPhase.DISCOVERING, keeper.machine.phase)

    def test_shared_deadline_does_not_cool_unverified_relay(self):
        relay = SimpleNamespace(
            endpoint="1.1.1.1:443", ip="1.1.1.1", port=443, country_short="RU"
        )
        clock = SimpleNamespace(value=0.0)

        class Provider:
            def __init__(self):
                self.failures = []

            def candidates(self):
                return [relay]

            def record_failure(self, item, reason):
                self.failures.append((item, reason))

        class DeadlineSoftEther:
            def verified_connection(self):
                return None

            def probe_tcp(self, _relay, **_kwargs):
                clock.value = 2.0
                return True

            def connect(self, _relay, **_kwargs):
                raise AssertionError("connect must not run after the shared deadline")

            def disconnect(self, **_kwargs):
                return True

        class Routes:
            def cleanup(self, **_kwargs):
                pass

        provider = Provider()
        keeper = KeeperService(
            config=SimpleNamespace(
                game_log_roots=(),
                target_hosts=("lobby.escapefromtarkov.ru",),
                failover_timeout_seconds=1,
                connect_timeout_seconds=18,
                disconnect_wait_seconds=1,
                tcp_probe_timeout_milliseconds=1500,
            ),
            candidate_provider=provider,
            softether=DeadlineSoftEther(),
            routes=Routes(),
        )
        with patch("tarkov_cis.keeper.time.monotonic", side_effect=lambda: clock.value):
            self.assertFalse(keeper.run_cycle())
        self.assertEqual([], provider.failures)
        self.assertEqual(KeeperPhase.RETRY_WAIT, keeper.status.phase)
        self.assertEqual(ConnectionPhase.DISCOVERING, keeper.machine.phase)
        self.assertEqual(0, keeper.status.failed_candidates)

    def test_handshake_timeout_at_shared_deadline_is_not_remote_failure(self):
        relay = SimpleNamespace(
            endpoint="1.1.1.1:443", ip="1.1.1.1", port=443, country_short="RU"
        )
        clock = SimpleNamespace(value=0.0)

        class Provider:
            def __init__(self):
                self.failures = []

            def candidates(self):
                return [relay]

            def record_failure(self, item, reason):
                self.failures.append((item, reason))

        class DeadlineSoftEther:
            def verified_connection(self):
                return None

            def probe_tcp(self, _relay, **_kwargs):
                return True

            def connect(self, _relay, **_kwargs):
                clock.value = 2.0
                raise SoftEtherError("handshake timed out")

            def disconnect(self, **_kwargs):
                return True

        class Routes:
            def cleanup(self, **_kwargs):
                pass

        provider = Provider()
        keeper = KeeperService(
            config=SimpleNamespace(
                game_log_roots=(),
                target_hosts=("lobby.escapefromtarkov.ru",),
                failover_timeout_seconds=1,
                connect_timeout_seconds=18,
                disconnect_wait_seconds=1,
                tcp_probe_timeout_milliseconds=1500,
            ),
            candidate_provider=provider,
            softether=DeadlineSoftEther(),
            routes=Routes(),
        )
        with patch("tarkov_cis.keeper.time.monotonic", side_effect=lambda: clock.value):
            self.assertFalse(keeper.run_cycle())
        self.assertEqual([], provider.failures)
        self.assertEqual(KeeperPhase.RETRY_WAIT, keeper.status.phase)
        self.assertEqual(0, keeper.status.failed_candidates)

    def test_existing_session_dns_failure_cleans_owned_routes(self):
        lease = VpnLease(12, "VPN", "10.1.0.2", "10.1.0.1")

        class ConnectedSoftEther:
            def verified_connection(self):
                return lease

        class Routes:
            def __init__(self):
                self.cleanup_calls = 0
                self.sync_calls = 0

            def cleanup(self, **_kwargs):
                self.cleanup_calls += 1

            def sync(self, *_args, **_kwargs):
                self.sync_calls += 1

        for resolution in ((), RuntimeError("DNS failed")):
            with self.subTest(resolution=repr(resolution)):
                routes = Routes()
                keeper = KeeperService(
                    config=SimpleNamespace(
                        game_log_roots=(),
                        target_hosts=("lobby.escapefromtarkov.ru",),
                        failover_timeout_seconds=10,
                    ),
                    candidate_provider=lambda: (),
                    softether=ConnectedSoftEther(),
                    routes=routes,
                )
                if isinstance(resolution, Exception):
                    resolver = patch(
                        "tarkov_cis.keeper.resolve_authorization_targets",
                        side_effect=resolution,
                    )
                else:
                    resolver = patch(
                        "tarkov_cis.keeper.resolve_authorization_targets",
                        return_value=resolution,
                    )
                with resolver:
                    self.assertFalse(keeper.run_cycle())
                self.assertEqual(1, routes.cleanup_calls)
                self.assertEqual(0, routes.sync_calls)
                self.assertEqual(ConnectionPhase.FAILED, keeper.machine.phase)
                self.assertEqual(KeeperPhase.FAILED, keeper.status.phase)

    def test_stop_shares_one_deadline_across_join_routes_and_disconnect(self):
        clock = SimpleNamespace(value=100.0)
        observed = SimpleNamespace(join_timeout=None, route_deadline=None, disconnect_timeout=None)

        class FinishedThread:
            def join(self, timeout):
                observed.join_timeout = timeout
                clock.value = 102.0

            def is_alive(self):
                return False

        class Routes:
            def cleanup(self, *, deadline=None):
                observed.route_deadline = deadline
                clock.value = 104.0

        class SoftEther:
            def disconnect(self, *, timeout):
                observed.disconnect_timeout = timeout
                return True

        keeper = KeeperService(
            config=SimpleNamespace(),
            candidate_provider=lambda: (),
            softether=SoftEther(),
            routes=Routes(),
        )
        keeper._thread = FinishedThread()
        with patch("tarkov_cis.keeper.time.monotonic", side_effect=lambda: clock.value):
            keeper.stop(timeout=5.0, disconnect_timeout=10.0)
        self.assertEqual(5.0, observed.join_timeout)
        self.assertEqual(105.0, observed.route_deadline)
        self.assertEqual(1.0, observed.disconnect_timeout)
        self.assertEqual(ConnectionPhase.DISCONNECTED, keeper.machine.phase)

    def test_stop_does_not_disconnect_after_total_deadline_expires(self):
        clock = SimpleNamespace(value=100.0)

        class Routes:
            def cleanup(self, *, deadline=None):
                self.deadline = deadline
                clock.value = 106.0

        class SoftEther:
            def __init__(self):
                self.disconnect_calls = 0

            def disconnect(self, **_kwargs):
                self.disconnect_calls += 1

        routes = Routes()
        softether = SoftEther()
        keeper = KeeperService(
            config=SimpleNamespace(),
            candidate_provider=lambda: (),
            softether=softether,
            routes=routes,
        )
        with patch("tarkov_cis.keeper.time.monotonic", side_effect=lambda: clock.value):
            with self.assertRaisesRegex(RuntimeError, "stop deadline expired"):
                keeper.stop(timeout=5.0)
        self.assertEqual(105.0, routes.deadline)
        self.assertEqual(0, softether.disconnect_calls)


class RouteDeadlineTests(unittest.TestCase):
    def test_expired_sync_deadline_runs_no_powershell_command(self):
        calls = []

        def runner(argv, **_kwargs):
            calls.append(tuple(argv))
            return result(argv)

        with tempfile.TemporaryDirectory() as temporary:
            manager = RouteManager(
                runner=runner,
                state_path=Path(temporary, "routes.json"),
            )
            lease = VpnLease(12, "VPN", "10.1.0.2", "10.1.0.1")
            with patch("tarkov_cis.routing.time.monotonic", return_value=100.0):
                with self.assertRaisesRegex(RouteError, "deadline expired"):
                    manager.sync(["203.0.113.10"], lease, deadline=99.0)
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
