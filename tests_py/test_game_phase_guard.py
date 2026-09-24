from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from tarkov_cis import cli
from tarkov_cis.config import AppConfig
from tarkov_cis.eft_logs import AuthorizationTarget, _read_tail
from tarkov_cis.game_phase import (
    GamePhase, GamePhaseSnapshot, PlayProtectionActivated,
    configured_game_phase_probe, detect_game_phase, is_eft_process_running,
)
from tarkov_cis.gui import _format_status
from tarkov_cis.health import HealthResult
from tarkov_cis.keeper import KeeperPhase, KeeperService
from tarkov_cis.models import ConnectionPhase
from tarkov_cis.process_runner import CommandResult
from tarkov_cis.routing import ManagedRoute, RouteManager
from tarkov_cis.softether import LocalResourceBusyError, SoftEtherClient, SoftEtherError, VpnLease


LEASE = VpnLease(12, "VPN", "10.0.0.2", "10.0.0.1")
TARGETS = (AuthorizationTarget("192.0.2.2", ("lobby.escapefromtarkov.com",)),)
RELAY = SimpleNamespace(endpoint="192.0.2.1:443", ip="192.0.2.1", port=443, country_short="RU")
LOGIN = GamePhaseSnapshot(phase=GamePhase.LOGIN, process_running=True)
MATCHING = GamePhaseSnapshot(phase=GamePhase.MATCHMAKING, process_running=True)
RAID = GamePhaseSnapshot(phase=GamePhase.RAID, process_running=True)


class GamePhaseDetectionTests(unittest.TestCase):
    def _log(self, content: str) -> Path:
        folder = Path(self.tmp.name)
        path = folder / "eft.log"
        path.write_text(content, encoding="utf-8")
        return path

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Fixtures must not start expiring three hours after the test was added.
        now = datetime(2026, 9, 24, 22, 30).astimezone().timestamp()
        self.clock = patch("tarkov_cis.game_phase.time.time", return_value=now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_character_selection_keeps_pre_raid_checks_enabled(self):
        self._log(
            "2026-09-24 21:56:03.677|1.1.5.1|Info|output|application|"
            "EFT.TarkovApplication:ShowCharacterSelectionScreen(CharacterSelectionDataResponse)\n"
        )
        snapshot = detect_game_phase(
            [self.tmp.name], process_checker=lambda: True
        )
        self.assertEqual(GamePhase.CHARACTER_SELECT, snapshot.phase)
        self.assertFalse(snapshot.phase is GamePhase.RAID)

    def test_real_raid_markers_lock_until_match_over(self):
        self._log(
            "2026-09-24 21:58:32.122|1.1.5.1|Info|output|application|TRACE-NetworkGameMatching G\n"
            "2026-09-24 21:59:14.809|1.1.5.1|Info|output|application|"
            "TRACE-NetworkGameCreate profileStatus: 'Status: Busy, RaidMode: Online, Ip: 95.1.2.3, Port: 17014'\n"
            "2026-09-24 22:00:19.109|1.1.5.1|Info|output|application|GameStarted:299.9 real:321.7\n"
        )
        snapshot = detect_game_phase(
            [self.tmp.name], process_checker=lambda: True
        )
        self.assertEqual(GamePhase.RAID_STARTED, snapshot.phase)
        self.assertEqual(("95.1.2.3", 17014), (snapshot.raid_ip, snapshot.raid_port))

        self._log(
            "2026-09-24 22:10:00.000|1.1.5.1|Info|output|push-notifications|"
            "Got notification | UserMatchOver\n"
        )
        ended = detect_game_phase([self.tmp.name], process_checker=lambda: True)
        self.assertEqual(GamePhase.POST_RAID, ended.phase)

    def test_game_started_survives_late_create_trace_and_match_over(self):
        self._log(
            "2026-09-24 22:00:00.000|Info|application|GameStarted:1\n"
            "2026-09-24 22:00:01.000|Debug|application|TRACE-NetworkGameCreate 6\n"
            "2026-09-24 22:01:00.000|Info|push-notifications|Got notification | UserMatchOver\n"
            "EFT.NetworkGameSession:NetworkGameCreate()\n"
        )
        snapshot = detect_game_phase([self.tmp.name], process_checker=lambda: True)
        self.assertEqual(GamePhase.POST_RAID, snapshot.phase)
        self.assertTrue(snapshot.raid_started_seen)

    def test_user_confirmed_marker_is_already_inside_raid(self):
        self._log(
            "2026-09-24 22:01:00.000|1.1.5.1|Info|output|push-notifications|"
            "Got notification | UserConfirmed {\"status\":\"Busy\",\"ip\":\"203.0.113.9\",\"port\":17000}\n"
        )
        snapshot = detect_game_phase([self.tmp.name], process_checker=lambda: True)
        self.assertEqual(GamePhase.RAID, snapshot.phase)
        self.assertEqual(("203.0.113.9", 17000), (snapshot.raid_ip, snapshot.raid_port))

    def test_stale_raid_marker_is_not_protected_after_game_exits(self):
        self._log(
            "2026-09-24 21:58:32.122|1.1.5.1|Info|output|application|"
            "TRACE-NetworkGameCreate profileStatus: 'Status: Busy, RaidMode: Online'\n"
        )
        snapshot = detect_game_phase(
            [self.tmp.name], process_checker=lambda: False
        )
        self.assertEqual(GamePhase.RAID, snapshot.phase)
        self.assertFalse(snapshot.process_running)

    def test_old_raid_marker_does_not_freeze_a_new_game_process(self):
        self._log(
            "2000-01-01 00:00:00.000|1.1.5.1|Info|output|application|"
            "TRACE-NetworkGameCreate profileStatus: 'Status: Busy, RaidMode: Online'\n"
        )
        snapshot = detect_game_phase([self.tmp.name], process_checker=lambda: True)
        self.assertEqual(GamePhase.UNKNOWN, snapshot.phase)
        self.assertTrue(snapshot.process_running)

    def test_stack_marker_keeps_its_header_time_not_file_mtime(self):
        self._log(
            "2026-09-24 21:58:00.000|Info|output|FrameTicks\n"
            "EFT.MainMenuShowOperation:Execute()\n"
            "2026-09-24 22:02:00.000|Info|output|unrelated late log entry\n"
        )
        application = Path(self.tmp.name) / "application.log"
        application.write_text("2026-09-24 22:00:00.000|Debug|application|GameStarted:1\n")
        snapshot = detect_game_phase([self.tmp.name], process_checker=lambda: True)
        self.assertEqual(GamePhase.RAID_STARTED, snapshot.phase)
        self.assertEqual(0, snapshot.observed_at.microsecond)

    def test_partial_stack_at_tail_start_cannot_unlock_raid(self):
        self._log("EFT.MainMenuShowOperation:Execute()\n2026-09-24 22:02:00.000|Info|noise\n")
        (Path(self.tmp.name) / "application.log").write_text(
            "2026-09-24 22:00:00.000|Debug|GameStarted:1\n"
        )
        snapshot = detect_game_phase([self.tmp.name], process_checker=lambda: True)
        self.assertEqual(GamePhase.RAID_STARTED, snapshot.phase)

    def test_menu_stack_during_character_authorization_is_not_lobby_ready(self):
        self._log(
            "2026-09-24 22:00:00.000|Info|output|ShowCharacterSelectionScreen\n"
            "2026-09-24 22:01:00.000|Warn|output|Quest condition unavailable\n"
            "EFT.MainMenuShowOperation:Init()\n"
            "EFT.MainMenuShowOperation:Execute()\n"
        )
        snapshot = detect_game_phase([self.tmp.name], process_checker=lambda: True)
        self.assertEqual(GamePhase.CHARACTER_SELECT, snapshot.phase)

    def test_gamestarted_stack_frame_is_not_raid_entry(self):
        self._log(
            "2026-09-24 22:00:00.000|Info|output|TRACE-NetworkGameCreate\n"
            "2026-09-24 22:01:00.000|Warn|output|unrelated warning\n"
            "EFT.GameStarted:MoveNext()\n"
        )
        snapshot = detect_game_phase([self.tmp.name], process_checker=lambda: True)
        self.assertEqual(GamePhase.RAID, snapshot.phase)

    def test_latest_launch_ignores_recent_unfinished_raid_from_prior_launch(self):
        old = Path(self.tmp.name) / "log_2026.09.24_21-00-00_1.0"
        new = Path(self.tmp.name) / "log_2026.09.24_22-05-00_1.0"
        old.mkdir()
        new.mkdir()
        (old / "application.log").write_text("2026-09-24 22:00:00.000|Debug|GameStarted:1\n")
        (new / "application.log").write_text("2026-09-24 22:05:00.000|Info|startup\n")
        snapshot = detect_game_phase([self.tmp.name], process_checker=lambda: True)
        self.assertEqual(GamePhase.UNKNOWN, snapshot.phase)
        self.assertEqual("2026.09.24_22-05-00", snapshot.session_id)

    def test_unpadded_midnight_hour_is_current_launch(self):
        old = Path(self.tmp.name) / "log_2026.09.24_23-59-57_1.0"
        new = Path(self.tmp.name) / "log_2026.09.25_0-23-51_1.0"
        old.mkdir()
        new.mkdir()
        (old / "application.log").write_text("2026-09-24 23:59:58.000|Debug|GameStarted:1\n")
        (new / "application.log").write_text("2026-09-25 00:23:52.000|Info|Successful login\n")
        snapshot = detect_game_phase([self.tmp.name], process_checker=lambda: True)
        self.assertEqual("2026.09.25_00-23-51", snapshot.session_id)
        self.assertEqual(GamePhase.LOGIN, snapshot.phase)
        self.assertFalse(snapshot.raid_started_seen)

    def test_endpoint_is_carried_forward_chronologically_across_log_files(self):
        path = self._log("2026-09-24 22:01:00.000|Debug|GameStarted:1\n")
        early = Path(self.tmp.name) / "application.log"
        early.write_text(
            "2026-09-24 22:00:00.000|Debug|TRACE-NetworkGameCreate Ip: 192.0.2.7, Port: 17000\n"
        )
        os.utime(early, (path.stat().st_mtime - 1, path.stat().st_mtime - 1))
        snapshot = detect_game_phase([self.tmp.name], process_checker=lambda: True)
        self.assertEqual(("192.0.2.7", 17000), (snapshot.raid_ip, snapshot.raid_port))

    def test_read_failure_is_not_reported_as_process_exit(self):
        runner = MagicMock(side_effect=subprocess.TimeoutExpired("tasklist", 1.5))
        self.assertIsNone(is_eft_process_running(runner=runner))
        self._log("2026-09-24 22:00:00.000|Debug|GameStarted:1\n")
        snapshot = detect_game_phase([self.tmp.name], process_checker=lambda: None)
        self.assertEqual(GamePhase.RAID_STARTED, snapshot.phase)
        self.assertIsNone(snapshot.process_running)

    def test_repeated_boundary_checks_reuse_unchanged_logs_but_see_new_bytes(self):
        path = self._log("2026-09-24 22:00:00.000|Info|Successful login\n")
        probe = configured_game_phase_probe(SimpleNamespace(game_log_roots=(self.tmp.name,)))
        with (
            patch("tarkov_cis.eft_logs.discover_log_roots", return_value=(Path(self.tmp.name),)),
            patch("tarkov_cis.game_phase.is_eft_process_running", return_value=True) as process,
            patch("tarkov_cis.game_phase.time.monotonic", return_value=100.0),
            patch("tarkov_cis.game_phase._read_tail", wraps=_read_tail) as read,
        ):
            self.assertEqual(GamePhase.LOGIN, probe().phase)
            self.assertEqual(GamePhase.LOGIN, probe().phase)
            read.assert_called_once()
            process.assert_called_once()
            path.write_text("2026-09-24 22:01:00.000|Debug|TRACE-NetworkGameMatching G\n")
            self.assertEqual(GamePhase.MATCHMAKING, probe().phase)
            self.assertEqual(2, read.call_count)


class KeeperGameProtectionTests(unittest.TestCase):
    def service(self):
        self.snapshot = LOGIN
        self.softether = MagicMock()
        self.softether.verified_connection.return_value = LEASE
        self.routes = MagicMock()
        self.provider = MagicMock(return_value=[RELAY, RELAY])
        self.health = MagicMock(return_value=HealthResult(True, "HTTP"))
        self.keeper = KeeperService(
            config=SimpleNamespace(pause_during_raid=True), candidate_provider=self.provider,
            softether=self.softether, routes=self.routes, health_probe=self.health,
            game_phase_probe=lambda: self.snapshot,
        )
        self.keeper._authorization_targets = MagicMock(return_value=TARGETS)
        return self.keeper

    def enter_matching(self, *args, **kwargs):
        self.snapshot = MATCHING
        return None

    def test_raid_protection_skips_all_network_reconciliation(self):
        softether = MagicMock()
        routes = MagicMock()
        config = SimpleNamespace(pause_during_raid=True)
        snapshot = GamePhaseSnapshot(
            phase=GamePhase.RAID,
            detail="TRACE-NetworkGameCreate：Raid 会话已建立",
            process_running=True,
        )
        keeper = KeeperService(
            config=config,
            candidate_provider=MagicMock(return_value=[]),
            softether=softether,
            routes=routes,
            game_phase_probe=lambda: snapshot,
        )

        self.assertTrue(keeper.run_cycle())
        softether.verified_connection.assert_not_called()
        softether.disconnect.assert_not_called()
        routes.cleanup.assert_not_called()
        self.assertEqual(KeeperPhase.PLAY_PROTECTED, keeper.status.phase)
        self.assertTrue(keeper.status.play_protected)
        self.assertEqual("raid", keeper.status.game_phase)


    def test_matching_also_freezes_relay_switching(self):
        softether = MagicMock()
        routes = MagicMock()
        keeper = KeeperService(
            config=SimpleNamespace(pause_during_raid=True),
            candidate_provider=MagicMock(return_value=[]),
            softether=softether,
            routes=routes,
            game_phase_probe=lambda: GamePhaseSnapshot(
                phase=GamePhase.MATCHMAKING,
                detail="匹配中",
                process_running=True,
            ),
        )

        self.assertTrue(keeper.run_cycle())
        softether.verified_connection.assert_not_called()
        self.assertEqual(KeeperPhase.PLAY_PROTECTED, keeper.status.phase)
        self.assertEqual("matchmaking", keeper.status.game_phase)

    def test_character_selection_does_not_skip_checks(self):
        softether = MagicMock()
        softether.verified_connection.return_value = None
        routes = MagicMock()
        keeper = KeeperService(
            config=SimpleNamespace(
                pause_during_raid=True,
                session_failure_threshold=3,
                health_failure_threshold=3,
                failed_cycle_retry_seconds=5,
                failed_cycle_backoff_max_seconds=120,
            ),
            candidate_provider=MagicMock(return_value=[]),
            softether=softether,
            routes=routes,
            game_phase_probe=lambda: GamePhaseSnapshot(
                phase=GamePhase.CHARACTER_SELECT,
                detail="等待选角色",
                process_running=True,
            ),
        )

        self.assertFalse(keeper.run_cycle())
        softether.verified_connection.assert_called_once()
        self.assertNotEqual(KeeperPhase.PLAY_PROTECTED, keeper.status.phase)

    def test_ready_relay_stays_stable_from_game_launch_through_lobby(self):
        keeper = self.service()
        keeper.machine.session_restored()
        keeper.machine.routes_applied()
        self.snapshot = GamePhaseSnapshot(phase=GamePhase.LOGIN, process_running=True)
        self.assertTrue(keeper.run_cycle())
        self.assertEqual(KeeperPhase.PLAY_PROTECTED, keeper.status.phase)
        self.softether.verified_connection.assert_not_called()
        self.softether.disconnect.assert_not_called()

    def test_matching_starts_during_session_read_no_cleanup_follows(self):
        keeper = self.service()
        self.softether.verified_connection.side_effect = self.enter_matching
        self.assertTrue(keeper.run_cycle())
        self.routes.cleanup.assert_not_called()
        self.softether.disconnect.assert_not_called()
        keeper._authorization_targets.assert_not_called()
        self.assertEqual(KeeperPhase.PLAY_PROTECTED, keeper.status.phase)

    def test_matching_starts_during_dns_no_route_changes_follow(self):
        keeper = self.service()
        def resolve():
            self.snapshot = MATCHING
            return TARGETS
        keeper._authorization_targets.side_effect = resolve
        self.assertTrue(keeper.run_cycle())
        self.routes.sync.assert_not_called()
        self.routes.cleanup.assert_not_called()
        self.health.assert_not_called()

    def test_matching_starts_during_sync_no_health_probe_follows(self):
        keeper = self.service()
        self.routes.sync.side_effect = self.enter_matching
        self.assertTrue(keeper.run_cycle())
        self.health.assert_not_called()
        self.softether.disconnect.assert_not_called()

    def test_matching_starts_during_route_error_reports_protection_not_failure(self):
        keeper = self.service()
        def failed_sync(*args, **kwargs):
            self.snapshot = MATCHING
            raise OSError("adapter changed during synchronization")
        self.routes.sync.side_effect = failed_sync
        self.assertTrue(keeper.run_cycle())
        self.assertEqual(KeeperPhase.PLAY_PROTECTED, keeper.status.phase)
        self.routes.cleanup.assert_not_called()
        self.softether.disconnect.assert_not_called()

    def test_matching_starts_during_health_failure_no_switch_follows(self):
        keeper = self.service()
        keeper._health_failures = 2
        def unhealthy(*args, **kwargs):
            self.snapshot = MATCHING
            return HealthResult(False, "timeout")
        self.health.side_effect = unhealthy
        self.assertTrue(keeper.run_cycle())
        self.routes.cleanup.assert_not_called()
        self.softether.disconnect.assert_not_called()
        self.provider.assert_not_called()

    def test_matching_starts_during_cleanup_no_disconnect_follows(self):
        keeper = self.service()
        self.softether.verified_connection.return_value = None
        self.routes.cleanup.side_effect = self.enter_matching
        self.assertTrue(keeper.run_cycle())
        self.softether.disconnect.assert_not_called()
        self.provider.assert_not_called()

    def test_matching_starts_during_catalog_no_candidate_is_probed(self):
        keeper = self.service()
        self.softether.verified_connection.return_value = None
        def candidates():
            self.snapshot = MATCHING
            return [RELAY]
        self.provider.side_effect = candidates
        self.assertTrue(keeper.run_cycle())
        self.softether.probe_tcp.assert_not_called()
        self.softether.connect.assert_not_called()

    def test_matching_starts_during_tcp_probe_no_connect_follows(self):
        keeper = self.service()
        self.softether.verified_connection.return_value = None
        def tcp_probe(*args, **kwargs):
            self.snapshot = MATCHING
            return True
        self.softether.probe_tcp.side_effect = tcp_probe
        self.assertTrue(keeper.run_cycle())
        self.softether.connect.assert_not_called()
        self.assertEqual(1, self.routes.cleanup.call_count)
        self.assertEqual(1, self.softether.disconnect.call_count)

    def test_matching_during_failed_connect_skips_reset_and_next_candidate(self):
        for error in (SoftEtherError("offline"), LocalResourceBusyError("busy"), RuntimeError("local")):
            with self.subTest(error=type(error).__name__):
                keeper = self.service()
                self.softether.verified_connection.return_value = None
                def connect(*args, **kwargs):
                    self.snapshot = MATCHING
                    raise error
                self.softether.connect.side_effect = connect
                self.assertTrue(keeper.run_cycle())
                self.assertEqual(1, self.routes.cleanup.call_count)
                self.assertEqual(1, self.softether.disconnect.call_count)
                self.assertEqual(1, self.softether.connect.call_count)
                self.provider.record_failure.assert_not_called()
                self.assertEqual(KeeperPhase.PLAY_PROTECTED, keeper.status.phase)

    def test_known_raid_remains_locked_when_tail_rolls_or_probe_fails(self):
        keeper = self.service()
        self.snapshot = RAID
        self.assertTrue(keeper.run_cycle())
        self.snapshot = GamePhaseSnapshot(process_running=True)
        self.assertTrue(keeper.run_cycle())
        keeper.game_phase_probe = MagicMock(side_effect=OSError("temporarily unreadable"))
        self.assertTrue(keeper.run_cycle())
        self.softether.verified_connection.assert_not_called()
        self.softether.disconnect.assert_not_called()

    def test_older_menu_marker_cannot_unlock_raid(self):
        keeper = self.service()
        stamp = datetime.now(timezone.utc)
        self.snapshot = GamePhaseSnapshot(phase=GamePhase.RAID, process_running=True, observed_at=stamp)
        keeper.run_cycle()
        self.snapshot = GamePhaseSnapshot(phase=GamePhase.MENU, process_running=True,
                                          observed_at=stamp - timedelta(seconds=1))
        self.assertTrue(keeper.run_cycle())
        self.softether.verified_connection.assert_not_called()

    def test_post_raid_keeps_connection_stable(self):
        keeper = self.service()
        keeper._health_failures = 2
        keeper._session_failures = 2
        self.snapshot = RAID
        keeper.run_cycle()
        self.snapshot = GamePhaseSnapshot(phase=GamePhase.POST_RAID, process_running=True)
        self.health.return_value = HealthResult(False, "timeout")
        self.assertTrue(keeper.run_cycle())
        self.assertEqual(0, keeper._health_failures)
        self.softether.verified_connection.assert_not_called()
        self.softether.disconnect.assert_not_called()

    def test_exit_or_new_launch_releases_previous_raid_latch(self):
        for next_snapshot in (
            GamePhaseSnapshot(process_running=False),
            GamePhaseSnapshot(process_running=True, session_id="new"),
        ):
            with self.subTest(snapshot=next_snapshot):
                keeper = self.service()
                self.snapshot = GamePhaseSnapshot(phase=GamePhase.RAID, process_running=True, session_id="old")
                keeper.run_cycle()
                self.snapshot = next_snapshot
                self.assertTrue(keeper.run_cycle())
                self.softether.verified_connection.assert_called_once()
                expected = KeeperPhase.PLAY_PROTECTED if next_snapshot.process_running else KeeperPhase.READY
                self.assertEqual(expected, keeper.status.phase)

    def test_protection_can_be_disabled_explicitly(self):
        keeper = self.service()
        keeper.config.pause_during_raid = False
        self.snapshot = RAID
        self.assertTrue(keeper.run_cycle())
        self.softether.verified_connection.assert_called_once()

    def test_post_raid_preserves_a_suspended_connect_state(self):
        keeper = self.service()
        keeper.machine.start()
        keeper.machine.candidate_selected(RELAY)
        keeper.machine.tcp_connected()
        self.snapshot = RAID
        keeper.run_cycle()
        self.snapshot = GamePhaseSnapshot(phase=GamePhase.POST_RAID, process_running=True)
        self.assertTrue(keeper.run_cycle())
        self.assertEqual(ConnectionPhase.VERIFYING_SESSION, keeper.machine.phase)


class RaidDisconnectTests(unittest.TestCase):
    def service(self, snapshot):
        self.snapshot = snapshot
        self.softether = MagicMock()
        self.softether.disconnect.return_value = True
        self.softether.verified_connection.return_value = LEASE
        self.routes = MagicMock()
        self.provider = MagicMock(return_value=[])
        self.keeper = KeeperService(
            config=SimpleNamespace(disconnect_at_raid=True, pause_during_raid=True),
            candidate_provider=self.provider, softether=self.softether,
            routes=self.routes, game_phase_probe=lambda: self.snapshot,
        )
        self.keeper._authorization_targets = lambda: TARGETS
        return self.keeper

    def test_menu_and_character_selection_keep_vpn_connected(self):
        keeper = self.service(GamePhaseSnapshot(
            phase=GamePhase.MENU, detail="MainMenu", process_running=True,
            session_id="launch-a",
        ))
        keeper.machine.session_restored()
        self.assertTrue(keeper.run_cycle())
        self.assertEqual(KeeperPhase.READY, keeper.status.phase)
        self.softether.disconnect.assert_not_called()
        self.snapshot = GamePhaseSnapshot(phase=GamePhase.CHARACTER_SELECT,
                                          process_running=True, session_id="launch-a")
        self.assertTrue(keeper.run_cycle())
        self.softether.disconnect.assert_not_called()
        self.provider.assert_not_called()

    def test_matching_and_pre_raid_keep_vpn_connected(self):
        keeper = self.service(GamePhaseSnapshot(phase=GamePhase.MATCHMAKING,
                                               process_running=True, session_id="launch-a"))
        self.assertTrue(keeper.run_cycle())
        self.snapshot = GamePhaseSnapshot(phase=GamePhase.RAID,
                                          process_running=True, session_id="launch-a")
        self.assertTrue(keeper.run_cycle())
        self.assertEqual(KeeperPhase.PLAY_PROTECTED, keeper.status.phase)
        self.softether.disconnect.assert_not_called()
        self.provider.assert_not_called()

    def test_game_started_disconnects_once_and_keeps_vpn_off(self):
        keeper = self.service(GamePhaseSnapshot(
            phase=GamePhase.RAID_STARTED, detail="GameStarted", process_running=True,
            session_id="launch-a",
        ))
        keeper.machine.session_restored()
        self.assertTrue(keeper.run_cycle())
        self.assertEqual(ConnectionPhase.DISCONNECTED, keeper.machine.phase)
        self.assertEqual(KeeperPhase.RAID_DIRECT, keeper.status.phase)
        self.routes.cleanup.assert_called_once()
        self.softether.disconnect.assert_called_once()
        self.assertTrue(keeper.run_cycle())
        self.softether.disconnect.assert_called_once()
        self.provider.assert_not_called()

    def test_restart_after_raid_stays_direct_during_results(self):
        keeper = self.service(GamePhaseSnapshot(
            phase=GamePhase.POST_RAID, process_running=True,
            session_id="launch-a", raid_started_seen=True,
        ))
        keeper.machine.session_restored()
        self.assertTrue(keeper.run_cycle())
        self.assertEqual(KeeperPhase.RAID_DIRECT, keeper.status.phase)
        self.softether.disconnect.assert_called_once()
        self.provider.assert_not_called()

    def test_game_started_during_probe_aborts_connection_and_cleans_up(self):
        keeper = self.service(LOGIN)
        self.softether.verified_connection.return_value = None
        self.provider.return_value = [RELAY]

        def enter_raid(*_args, **_kwargs):
            self.snapshot = GamePhaseSnapshot(phase=GamePhase.RAID_STARTED,
                                              process_running=True, session_id="launch-a")
            return True

        self.softether.probe_tcp.side_effect = enter_raid
        self.assertTrue(keeper.run_cycle())
        self.softether.connect.assert_not_called()
        self.assertEqual(KeeperPhase.RAID_DIRECT, keeper.status.phase)
        self.assertEqual(ConnectionPhase.DISCONNECTED, keeper.machine.phase)

    def test_game_exit_reopens_login_window(self):
        keeper = self.service(GamePhaseSnapshot(phase=GamePhase.RAID_STARTED,
                                                process_running=True, session_id="launch-a"))
        self.assertTrue(keeper.run_cycle())
        self.snapshot = GamePhaseSnapshot(process_running=False, session_id="launch-a")
        self.softether.verified_connection.return_value = None
        self.assertFalse(keeper.run_cycle())
        self.provider.assert_called_once()

    def test_failed_raid_cleanup_retries_without_discovery(self):
        keeper = self.service(GamePhaseSnapshot(phase=GamePhase.RAID_STARTED,
                                                process_running=True, session_id="launch-a"))
        self.softether.disconnect.side_effect = [False, True]
        self.assertFalse(keeper.run_cycle())
        self.assertEqual(KeeperPhase.FAILED, keeper.status.phase)
        self.provider.assert_not_called()
        self.assertTrue(keeper.run_cycle())
        self.assertEqual(KeeperPhase.RAID_DIRECT, keeper.status.phase)
        self.assertEqual(2, self.softether.disconnect.call_count)

class AdapterPlayProtectionTests(unittest.TestCase):
    def test_connect_guard_stops_reconfiguration_between_adapter_commands(self):
        snapshot = [LOGIN]
        commands = []
        def runner(argv, **kwargs):
            commands.append(argv)
            if "AccountList" in argv:
                snapshot[0] = MATCHING
                return CommandResult(tuple(argv), 0, "Tarkov-CIS-PlayOnly", "")
            return CommandResult(tuple(argv), 0, "", "")
        softether = SoftEtherClient(runner=runner)
        KeeperService(config=SimpleNamespace(), candidate_provider=lambda: [],
                      softether=softether, routes=MagicMock(), game_phase_probe=lambda: snapshot[0])
        with self.assertRaises(PlayProtectionActivated):
            softether.connect(RELAY)
        self.assertTrue(any("AccountList" in argv for argv in commands))
        self.assertFalse(any("AccountSet" in argv or "AccountConnect" in argv for argv in commands))
        self.assertEqual(1, sum("AccountDisconnect" in argv for argv in commands))

    def test_connect_timeout_does_not_disconnect_after_matching_begins(self):
        snapshot = [LOGIN]
        clock = [0.0]
        commands = []
        def runner(argv, **kwargs):
            commands.append(argv)
            return CommandResult(tuple(argv), 0, "", "")
        def advance(_seconds):
            clock[0] = 2.0
            snapshot[0] = MATCHING
        softether = SoftEtherClient(runner=runner)
        KeeperService(config=SimpleNamespace(), candidate_provider=lambda: [],
                      softether=softether, routes=MagicMock(), game_phase_probe=lambda: snapshot[0])
        with (
            patch.object(softether, "ensure_account"),
            patch("tarkov_cis.softether.time.monotonic", side_effect=lambda: clock[0]),
            patch("tarkov_cis.softether.time.sleep", side_effect=advance),
            self.assertRaises(PlayProtectionActivated),
        ):
            softether.connect(RELAY, timeout=1.0)
        self.assertEqual(1, sum("AccountDisconnect" in argv for argv in commands))

    def test_route_cleanup_stops_between_routes_and_preserves_ownership(self):
        snapshot = [LOGIN]
        commands = []
        def runner(argv, **kwargs):
            commands.append(argv)
            snapshot[0] = MATCHING
            return CommandResult(tuple(argv), 0, "", "")
        with tempfile.TemporaryDirectory() as folder:
            routes = RouteManager(runner=runner, state_path=Path(folder) / "routes.json")
            routes._managed.update({ManagedRoute("192.0.2.2", 12, "10.0.0.1"),
                                    ManagedRoute("192.0.2.3", 12, "10.0.0.1")})
            KeeperService(config=SimpleNamespace(), candidate_provider=lambda: [],
                          softether=MagicMock(), routes=routes, game_phase_probe=lambda: snapshot[0])
            with self.assertRaises(PlayProtectionActivated):
                routes.cleanup()
            self.assertEqual(1, len(commands))
            self.assertEqual(1, len(routes.managed))


class StatusPlayProtectionTests(unittest.TestCase):
    def test_status_does_not_probe_or_report_disconnect_during_protection(self):
        runtime = SimpleNamespace(softether=MagicMock(), routes=SimpleNamespace(managed=()))
        persisted = {"play_protected": True, "game_phase": "raid", "vpn_ipv4": LEASE.ipv4}
        with (
            patch("tarkov_cis.cli._read_config", return_value=AppConfig()),
            patch("tarkov_cis.cli._scheduled_task_info", return_value={"state": "Running", "implementation": "python"}),
            patch("tarkov_cis.cli._control_paths", return_value=(Path("pid"), Path("stop"))),
            patch("tarkov_cis.cli._read_json", side_effect=[{}, persisted]),
            patch("tarkov_cis.cli._pid_state_matches_live_process", return_value=True),
            patch("tarkov_cis.cli._status_matches_pid_state", return_value=True),
            patch("tarkov_cis.cli.build_runtime", return_value=runtime),
        ):
            observed = cli.status_snapshot(Path("config.json"))
        runtime.softether.verified_connection.assert_not_called()
        self.assertIsNone(observed["vpn_verified"])
        self.assertEqual(LEASE.ipv4, observed["vpn_ipv4"])
        self.assertIn("VPN 探测已暂停", _format_status(observed))
        self.assertNotIn("VPN 未连接", _format_status(observed))


if __name__ == "__main__":
    unittest.main()
