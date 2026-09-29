from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest

from support import relay

from tarkov_cis.config import Config
from tarkov_cis.game import GameSnapshot, Phase as GamePhase
from tarkov_cis.keeper import Keeper, Phase
from tarkov_cis.models import VpnLease
from tarkov_cis.relays import KnownGood
from tarkov_cis.routes import RouteError
from tarkov_cis.softether import LocalBusyError, SoftEtherError

LEASE = VpnLease(63, "10.211.1.2", "10.211.254.254")
MENU = GameSnapshot(GamePhase.CHARACTER_SELECT, "选角色", process_running=True)
NO_GAME = GameSnapshot()
MATCHING = GameSnapshot(GamePhase.MATCHMAKING, "匹配中", process_running=True)


class FakeVpn:
    def __init__(self):
        self.current: VpnLease | None = None
        self.log: list[str] = []
        self.failing: set[str] = set()
        self.unreachable: set[str] = set()
        self.busy = False
        self.on_connect = None

    def lease(self):
        self.log.append("lease")
        return self.current

    def probe_tcp(self, relay, *, timeout):
        return relay.endpoint not in self.unreachable

    def connect(self, relay, *, timeout, guard):
        guard()
        self.log.append(f"connect {relay.endpoint}")
        if self.on_connect:
            self.on_connect()
            guard()
        if self.busy:
            raise LocalBusyError("busy")
        if relay.endpoint in self.failing:
            raise SoftEtherError("handshake failed")
        self.current = LEASE
        return LEASE

    def disconnect(self, *, timeout, guard=lambda: None):
        guard()
        self.log.append("disconnect")
        self.current = None
        return True


class FakeRoutes:
    def __init__(self):
        self.owned: tuple = ()
        self.log: list[str] = []
        self.fail = False

    def sync(self, ips, lease, *, guard):
        guard()
        if self.fail:
            raise RouteError("boom")
        self.owned = tuple(ips)
        self.log.append("sync")

    def cleanup(self, *, guard=lambda: None):
        guard()
        self.owned = ()
        self.log.append("cleanup")


class KeeperTests(unittest.TestCase):
    def setUp(self):
        self.vpn = FakeVpn()
        self.routes = FakeRoutes()
        self.relays = [relay("192.0.2.1"), relay("192.0.2.2", country="UA")]
        self.ips: tuple[str, ...] = ("198.51.100.1",)
        self.game = NO_GAME
        self.catalog_calls = 0
        self.config = Config()
        self.keeper = self.make()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, **config) -> Keeper:
        if config:
            self.config = Config(**config)

        def catalog():
            self.catalog_calls += 1
            if isinstance(self.relays, Exception):
                raise self.relays
            return list(self.relays)

        return Keeper(
            self.config, vpn=self.vpn, routes=self.routes, catalog=catalog,
            targets=lambda: self.ips, game=lambda: self.game,
        )

    def test_first_step_connects_best_candidate_and_routes(self):
        wait = self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.READY)
        self.assertEqual(self.keeper.status.relay, "192.0.2.1:443")
        self.assertEqual(self.keeper.status.country, "RU")
        self.assertEqual(self.routes.owned, self.ips)
        self.assertEqual(wait, self.config.refresh_seconds)

    def test_failed_and_unreachable_relays_cool_down_and_next_is_tried(self):
        self.relays = [relay("192.0.2.1"), relay("192.0.2.2", country="UA"), relay("192.0.2.3", country="KZ")]
        self.vpn.unreachable = {"192.0.2.1:443"}
        self.vpn.failing = {"192.0.2.2:443"}
        self.keeper.step()
        self.assertEqual(self.keeper.status.relay, "192.0.2.3:443")
        self.assertTrue(self.keeper.cooldown.is_cooling(self.relays[0]))
        self.assertTrue(self.keeper.cooldown.is_cooling(self.relays[1]))

    def test_all_candidates_failing_backs_off(self):
        self.vpn.failing = {r.endpoint for r in self.relays}
        first = self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.WAITING)
        self.config = Config(failed_cycle_retry_seconds=10, failed_cycle_backoff_max_seconds=60)
        keeper = self.make(failed_cycle_retry_seconds=10, failed_cycle_backoff_max_seconds=60)
        waits = [keeper.step() for _ in range(4)]
        self.assertEqual(first, 10)
        self.assertEqual(waits, [10, 20, 40, 60])

    def test_local_busy_does_not_cool_the_relay(self):
        self.vpn.busy = True
        self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.WAITING)
        self.assertFalse(self.keeper.cooldown.is_cooling(self.relays[0]))

    def test_catalog_failure_waits(self):
        self.relays = RuntimeError("offline")
        self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.WAITING)
        self.assertIn("offline", self.keeper.status.detail)

    def test_healthy_session_only_resyncs_routes(self):
        self.keeper.step()
        self.vpn.log.clear()
        self.keeper.step()
        self.assertEqual(self.vpn.log, ["lease"])
        self.assertEqual(self.catalog_calls, 1)

    def test_ready_relay_is_frozen_while_the_game_runs(self):
        self.keeper.step()
        self.game = MENU
        self.vpn.log.clear()
        self.routes.log.clear()
        self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.PROTECTED)
        self.assertEqual(self.vpn.log, [])
        self.assertEqual(self.routes.log, [])

    def test_without_a_ready_relay_the_menu_still_allows_connecting(self):
        self.game = MENU
        self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.READY)

    def test_match_blocks_even_the_initial_connection(self):
        self.game = MATCHING
        self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.PROTECTED)
        self.assertEqual(self.vpn.log, [])

    def test_match_starting_mid_failover_stops_at_the_next_command(self):
        self.relays = [relay("192.0.2.1"), relay("192.0.2.2", country="UA")]
        self.vpn.failing = {"192.0.2.1:443"}

        def match_starts():
            self.game = MATCHING

        self.vpn.on_connect = match_starts
        self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.PROTECTED)
        self.assertEqual([entry for entry in self.vpn.log if entry.startswith("connect")], ["connect 192.0.2.1:443"])
        self.assertEqual(self.vpn.log[-1], "connect 192.0.2.1:443")

    def test_protection_can_be_disabled(self):
        keeper = self.make(pause_during_raid=False)
        self.game = MATCHING
        keeper.step()
        self.assertEqual(keeper.status.phase, Phase.READY)

    def test_brief_session_loss_is_tolerated_before_switching(self):
        keeper = self.make(session_failure_threshold=3)
        keeper.step()
        self.vpn.current = None
        keeper.step()
        keeper.step()
        self.assertEqual(keeper.status.phase, Phase.WAITING)
        self.assertEqual(self.catalog_calls, 1)
        keeper.step()
        self.assertEqual(self.catalog_calls, 2)
        self.assertEqual(keeper.status.phase, Phase.READY)

    def test_empty_dns_keeps_existing_routes_but_never_adds_nothing(self):
        self.keeper.step()
        self.ips = ()
        self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.WAITING)
        self.assertEqual(self.routes.owned, ("198.51.100.1",))
        self.routes.owned = ()
        self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.FAILED)

    def test_route_failure_is_reported_and_retried_without_dropping_the_session(self):
        self.routes.fail = True
        self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.FAILED)
        self.routes.fail = False
        self.vpn.log.clear()
        self.keeper.step()
        self.assertEqual(self.keeper.status.phase, Phase.READY)
        self.assertNotIn("disconnect", self.vpn.log)

    def test_shutdown_cleans_up_even_during_a_match(self):
        self.keeper.step()
        self.game = MATCHING
        self.keeper.shutdown()
        self.assertEqual(self.routes.owned, ())
        self.assertIsNone(self.vpn.current)
        self.assertEqual(self.keeper.status.phase, Phase.STOPPED)

    def test_connected_relay_is_remembered_and_used_when_the_catalog_fails(self):
        known = KnownGood(Path(self.tmp.name) / "known-good.json", lifetime_hours=48)
        keeper = Keeper(
            self.config, vpn=self.vpn, routes=self.routes, catalog=lambda: self.relays,
            targets=lambda: self.ips, game=lambda: self.game, known_good=known,
        )
        keeper.step()
        self.assertEqual([r.endpoint for r in known.fresh()], ["192.0.2.1:443"])

        def offline():
            raise RuntimeError("offline")

        self.vpn.current = None
        restarted = Keeper(
            self.config, vpn=self.vpn, routes=self.routes, catalog=offline,
            targets=lambda: self.ips, game=lambda: self.game,
            known_good=KnownGood(Path(self.tmp.name) / "known-good.json", lifetime_hours=48),
        )
        restarted.step()
        self.assertEqual(restarted.status.phase, Phase.READY)
        self.assertEqual(restarted.status.relay, "192.0.2.1:443")

    def test_run_loop_exits_on_stop_and_shuts_down(self):
        stop = threading.Event()
        statuses = []
        keeper = Keeper(
            self.config, vpn=self.vpn, routes=self.routes, catalog=lambda: self.relays,
            targets=lambda: self.ips, game=lambda: self.game,
            publish=lambda status: (statuses.append(status.phase), stop.set() if status.phase is Phase.READY else None),
        )
        keeper.run(stop)
        self.assertEqual(statuses[0], Phase.STARTING)
        self.assertIn(Phase.READY, statuses)
        self.assertEqual(statuses[-1], Phase.STOPPED)


if __name__ == "__main__":
    unittest.main()
