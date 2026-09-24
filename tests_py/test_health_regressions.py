from pathlib import Path
import ssl
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from tarkov_cis.health import HealthResult, probe_authorization
from tarkov_cis.eft_logs import AuthorizationTarget, discover_log_roots
from tarkov_cis.keeper import KeeperService, KeeperPhase
from tarkov_cis.softether import VpnLease
from tarkov_cis.routing import (
    ManagedRoute,
    OriginalDefaultRoute,
    RouteManager,
    RouteError,
)
from tarkov_cis.process_runner import CommandResult

TARGETS = (AuthorizationTarget('192.0.2.2', ('lobby.escapefromtarkov.com',)),)
LEASE = VpnLease(12, 'VPN', '10.0.0.2', '10.0.0.1')


class HealthRegressionTests(unittest.TestCase):
    def test_real_default_context_verifies_certificates_and_hostname(self):
        contexts = []
        factory = ssl.create_default_context
        def capture():
            context = factory()
            contexts.append(context)
            return context
        with patch('tarkov_cis.health.ssl.create_default_context', side_effect=capture), \
             patch('tarkov_cis.health.socket.create_connection', side_effect=TimeoutError):
            self.assertFalse(probe_authorization(TARGETS).ok)
        self.assertEqual(ssl.CERT_REQUIRED, contexts[0].verify_mode)
        self.assertTrue(contexts[0].check_hostname)

    def test_exact_ip_sni_anonymous_http_and_empty_response(self):
        with patch('tarkov_cis.health.ssl.create_default_context') as factory, \
             patch('tarkov_cis.health.socket.create_connection') as connect:
            tls = factory.return_value.wrap_socket.return_value.__enter__.return_value
            tls.recv.return_value = b'HTTP/1.1 403 Forbidden\r\n'
            self.assertTrue(probe_authorization(TARGETS).ok)
            self.assertEqual(('192.0.2.2', 443), connect.call_args.args[0])
            self.assertEqual('lobby.escapefromtarkov.com', factory.return_value.wrap_socket.call_args.kwargs['server_hostname'])
            request = tls.sendall.call_args.args[0]
            self.assertTrue(request.startswith(b'HEAD / HTTP/1.1'))
            self.assertNotIn(b'Authorization:', request)
            tls.recv.return_value = b''
            self.assertFalse(probe_authorization(TARGETS).ok)

    def test_one_dark_authorization_target_does_not_drop_a_healthy_session(self):
        targets = (
            AuthorizationTarget('192.0.2.2', ('gw-pvp-season.escapefromtarkov.ru',)),
            AuthorizationTarget('192.0.2.3', ('lobby.escapefromtarkov.ru',)),
        )
        with patch('tarkov_cis.health.ssl.create_default_context') as factory, \
             patch('tarkov_cis.health.socket.create_connection') as connect:
            raw = MagicMock()
            raw.__enter__.return_value = raw
            tls = MagicMock()
            tls.__enter__.return_value = tls
            tls.recv.return_value = b'HTTP/1.1 403 Forbidden\r\n'
            factory.return_value.wrap_socket.return_value = tls
            connect.side_effect = [TimeoutError(), raw]

            result = probe_authorization(targets, timeout=2.0)

        self.assertTrue(result.ok)
        self.assertIn('1 target(s) skipped', result.detail)
        self.assertEqual(2, connect.call_count)

        # Each target keeps the full per-cycle socket budget; the probes run
        # concurrently, so a dark target cannot starve the later target.
        self.assertTrue(
            all(1.8 <= call.kwargs['timeout'] <= 2.01 for call in connect.call_args_list)
        )

    def service(self, probe):
        softether = MagicMock()
        softether.verified_connection.return_value = LEASE
        routes = MagicMock()
        provider = MagicMock(return_value=[])
        service = KeeperService(config=SimpleNamespace(), candidate_provider=provider,
                                softether=softether, routes=routes, health_probe=probe)
        service._authorization_targets = MagicMock(return_value=TARGETS)
        return service, softether, routes, provider

    def test_three_failures_then_bounded_discovery_not_one_failure(self):
        service, softether, routes, provider = self.service(lambda *a, **kw: HealthResult(False, 'empty reply'))
        self.assertFalse(service.run_cycle())
        self.assertFalse(service.run_cycle())
        softether.disconnect.assert_not_called()
        self.assertFalse(service.run_cycle())
        provider.assert_called_once()
        softether.disconnect.assert_called_once()
        self.assertEqual(KeeperPhase.RETRY_WAIT, service.status.phase)

    def test_success_resets_failure_threshold(self):
        probe = MagicMock(side_effect=[HealthResult(False, 'timeout'), HealthResult(True, 'HTTP'), HealthResult(False, 'timeout')])
        service, softether, _, _ = self.service(probe)
        self.assertFalse(service.run_cycle())
        self.assertTrue(service.run_cycle())
        self.assertFalse(service.run_cycle())
        softether.disconnect.assert_not_called()
        self.assertEqual(1, service._health_failures)

    def test_transient_session_loss_keeps_the_virtual_adapter_up(self):
        service, softether, _, provider = self.service(lambda *a, **kw: HealthResult(True, 'HTTP'))
        self.assertTrue(service.run_cycle())
        softether.verified_connection.side_effect = [None, None, None]

        self.assertFalse(service.run_cycle())
        self.assertFalse(service.run_cycle())
        softether.disconnect.assert_not_called()

        # The third consecutive miss is the first point at which discovery is
        # allowed.  An empty provider keeps this test entirely offline.
        self.assertFalse(service.run_cycle())
        softether.disconnect.assert_called_once()
        provider.assert_called_once()

    def test_dns_empty_preserves_last_good_routes_and_is_not_ready(self):
        service, _, routes, _ = self.service(lambda *a, **kw: HealthResult(True, 'HTTP'))
        self.assertTrue(service.run_cycle())
        service._authorization_targets.return_value = ()
        self.assertFalse(service.run_cycle())
        routes.cleanup.assert_not_called()
        self.assertEqual(1, routes.sync.call_count)
        self.assertEqual(KeeperPhase.RETRY_WAIT, service.status.phase)

    def test_route_manager_empty_set_does_not_issue_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = MagicMock()
            routes = RouteManager(runner=runner, state_path=Path(folder)/'routes.json')
            with self.assertRaises(RouteError):
                routes.sync([], LEASE)
            runner.assert_not_called()

    def test_cleanup_is_idempotent_when_softether_already_removed_routes(self):
        commands = []

        def runner(argv, **_kwargs):
            commands.append(argv[-1])
            return CommandResult(tuple(argv), 0, '', '')

        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / 'routes.json'
            routes = RouteManager(runner=runner, state_path=state)
            routes._managed.add(ManagedRoute('192.0.2.2', 12, '10.0.0.1'))
            routes._original_defaults.add(OriginalDefaultRoute(12, '10.0.0.1', 37))
            routes._save_state()

            routes.cleanup()

            self.assertEqual((), routes.managed)
            self.assertFalse(state.exists())
            self.assertEqual(2, len(commands))
            self.assertTrue(all('if($r.Count -eq 0){exit 0}' in script for script in commands))

    def test_dynamic_log_discovery_does_not_need_fixed_private_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'GAME'/'Tarkov'/'Logs'
            root.mkdir(parents=True)
            self.assertIn(root, discover_log_roots([], drive_roots=[folder]))

    def test_metric_snapshot_survives_repeated_sync_and_process_restart(self):
        current = [37]
        commands = []
        def runner(argv, **kwargs):
            script = argv[-1]
            commands.append(script)
            output = ''
            code = 0
            if 'Select-Object InterfaceIndex,NextHop,RouteMetric' in script:
                output = '{"InterfaceIndex":12,"NextHop":"10.0.0.1","RouteMetric":%d}' % current[0]
            elif '$ranked=@(Get-NetRoute' in script:
                output = '{"InterfaceIndex":7}'
            elif 'Set-NetRoute -PolicyStore ActiveStore -RouteMetric 9000' in script:
                current[0] = 9000
            elif 'Set-NetRoute -PolicyStore ActiveStore -RouteMetric 37' in script:
                current[0] = 37
            elif script.startswith("$ErrorActionPreference='SilentlyContinue'"):
                code = 1
            return CommandResult(tuple(argv), code, output, '')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)/'routes.json'
            manager = RouteManager(runner=runner, state_path=state)
            manager.sync(['192.0.2.2'], LEASE)
            manager.sync(['192.0.2.2'], LEASE)
            self.assertEqual(9000, current[0])
            RouteManager(runner=runner, state_path=state).cleanup()
            self.assertEqual(37, current[0])
            self.assertFalse(state.exists())

    def test_task_compatibility_and_metric_handoff_contract(self):
        source = (ROOT/'src/Tarkov-CisRouteKeeper.ps1').read_text(encoding='utf-8-sig')
        self.assertIn("'Run', 'Install'", source)
        self.assertIn("..\\scripts\\python-task.ps1", source)
        self.assertIn('-DeferRouteProtection', source)
        wrapper = (ROOT/'scripts/python-task.ps1').read_text(encoding='utf-8-sig')
        self.assertIn("'-Action', 'Stop', '-Legacy'", wrapper)
        self.assertIn("if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'Tarkov-CIS-Python.py')))", wrapper)

    @unittest.skipUnless(shutil.which('powershell.exe'), 'Windows PowerShell required')
    def test_actual_legacy_filename_dispatches_to_inert_python_wrapper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'src').mkdir()
            (root/'scripts').mkdir()
            entry = root/'src/Tarkov-CisRouteKeeper.ps1'
            entry.write_bytes((ROOT/'src/Tarkov-CisRouteKeeper.ps1').read_bytes())
            (root/'scripts/python-task.ps1').write_text(
                "param($Action,$ConfigPath)\nWrite-Output ('CANONICAL:'+$Action)\nexit 0", encoding='utf-8')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-File', str(entry),
                                     '-Action', 'Run', '-ConfigPath', str(root/'config.json')],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('CANONICAL:Run', result.stdout)
            (root/'src/Tarkov-CisRouteKeeper.state.json').write_text('{}')
            blocked = subprocess.run(['powershell.exe', '-NoProfile', '-File', str(entry),
                                      '-Action', 'Run'], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(0, blocked.returncode)
            self.assertNotIn('CANONICAL:Run', blocked.stdout)


if __name__ == '__main__':
    unittest.main()
