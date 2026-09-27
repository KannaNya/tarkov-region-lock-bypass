"""The background loop: keep one CIS relay up and the /32 routes in place.

Each step is one of:

* **protected** – a match is running (or the game runs with a ready relay):
  touch nothing, not even read-only vpncmd probes;
* **maintain** – the session is up: re-sync the /32 routes;
* **grace** – the session looks gone, but tolerate a few misses before
  tearing down a relay that may only be briefly unreadable;
* **failover** – disconnect, fetch catalogs and try candidates in order.

Before every state-changing command the loop re-checks the game phase, so
matchmaking starting halfway through a failover stops it at the next command
instead of disconnecting the player.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import time
from typing import Callable, Protocol

from .config import Config
from .game import GameSnapshot
from .models import Relay, VpnLease
from .relays import FailureCooldown, KnownGood, order_candidates
from .routes import RouteError, RouteManager
from .softether import LocalBusyError, SoftEther, SoftEtherError


class Phase(str, Enum):
    STARTING = "starting"
    READY = "ready"
    PROTECTED = "protected"
    DISCOVERING = "discovering"
    CONNECTING = "connecting"
    WAITING = "waiting"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class Status:
    phase: Phase = Phase.STARTING
    detail: str = "正在启动"
    relay: str = ""
    country: str = ""
    vpn_ipv4: str = ""
    route_count: int = 0
    game_phase: str = "unknown"
    game_detail: str = ""
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        data = asdict(self)
        data["phase"] = self.phase.value
        return data


class StopSignal(Protocol):
    def is_set(self) -> bool: ...
    def wait(self, timeout: float) -> bool: ...


class Paused(Exception):
    """A match started mid-operation; unwind without any cleanup."""


class Keeper:
    def __init__(
        self,
        config: Config,
        *,
        vpn: SoftEther,
        routes: RouteManager,
        catalog: Callable[[], list[Relay]],
        targets: Callable[[], tuple[str, ...]],
        game: Callable[[], GameSnapshot],
        known_good: KnownGood | None = None,
        log: Callable[[str], None] = lambda _message: None,
        publish: Callable[[Status], None] = lambda _status: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.vpn = vpn
        self.routes = routes
        self.catalog = catalog
        self.targets = targets
        self.game = game
        self.known_good = known_good
        self.log = log
        self._publish = publish
        self.clock = clock
        self.cooldown = FailureCooldown(config.failure_cooldown_minutes * 60, clock=clock)
        self.status = Status()
        self._ready = False
        self._misses = 0
        self._failed_rounds = 0
        self._relay: Relay | None = None
        self._last_game = GameSnapshot()
        self._stop: StopSignal | None = None

    # --- status ---------------------------------------------------------------

    def _set(self, phase: Phase, detail: str, **changes) -> None:
        changed = (phase, detail) != (self.status.phase, self.status.detail)
        self.status = replace(
            self.status,
            phase=phase,
            detail=detail,
            game_phase=self._last_game.phase.value,
            game_detail=self._last_game.detail,
            updated_at=datetime.now(timezone.utc).isoformat(),
            **changes,
        )
        if changed:
            self.log(f"[{phase.value}] {detail}")
        try:
            self._publish(self.status)
        except OSError as exc:
            self.log(f"[warning] 无法写入状态文件: {exc}")

    # --- game protection ------------------------------------------------------

    def _read_game(self) -> GameSnapshot:
        try:
            self._last_game = self.game()
        except Exception as exc:  # a broken probe must not crash the keeper
            self.log(f"[warning] 游戏阶段读取失败: {exc}")
        return self._last_game

    def _protected(self, snapshot: GameSnapshot) -> bool:
        if not self.config.pause_during_raid:
            return False
        # From matchmaking to settlement nothing may change; and once a relay
        # is ready it stays untouched for as long as the game is open.
        return snapshot.in_match or (self._ready and snapshot.process_running is True)

    def _guard(self) -> None:
        if self._protected(self._read_game()):
            raise Paused()

    # --- one cycle ------------------------------------------------------------

    def step(self) -> float:
        """Run one cycle and return the seconds to wait before the next."""

        try:
            if self._protected(self._read_game()):
                self._misses = 0
                label = "Raid" if self._last_game.in_match else "游戏"
                self._set(Phase.PROTECTED, f"{label}保护中：暂停一切 VPN 维护（{self._last_game.detail}）")
                return self.config.refresh_seconds
            lease = self.vpn.lease()
            if lease is not None:
                self._misses = 0
                return self._maintain(lease)
            if self._ready and self._misses + 1 < self.config.session_failure_threshold:
                self._misses += 1
                self._set(
                    Phase.WAITING,
                    f"SoftEther 会话暂时读不到 {self._misses}/{self.config.session_failure_threshold}，暂不切换",
                )
                return self.config.refresh_seconds
            return self._failover()
        except Paused:
            self._set(Phase.PROTECTED, f"游戏保护中：已中止当前操作（{self._last_game.detail}）")
            return self.config.refresh_seconds

    def _maintain(self, lease: VpnLease) -> float:
        ips = self.targets()
        if not ips:
            if self.routes.owned:
                self._set(Phase.WAITING, "DNS 暂无结果；保留现有鉴权路由，稍后重试")
                return self.config.failed_cycle_retry_seconds
            self._set(Phase.FAILED, "没有解析到任何 lobby/gw-pvp/WSN 鉴权地址；未添加路由")
            return self._backoff()
        try:
            self.routes.sync(ips, lease, guard=self._guard)
        except (RouteError, OSError) as exc:
            self._set(Phase.FAILED, f"鉴权路由同步失败: {exc}")
            return self._backoff()
        self._ready = True
        self._failed_rounds = 0
        relay = self._relay
        self._set(
            Phase.READY,
            "CIS 会话和鉴权路由已就绪",
            relay=relay.endpoint if relay else self.status.relay,
            country=relay.country if relay else self.status.country,
            vpn_ipv4=lease.ipv4,
            route_count=len(self.routes.owned),
        )
        return self.config.refresh_seconds

    def _failover(self) -> float:
        config = self.config
        deadline = self.clock() + config.failover_timeout_seconds
        self._ready = False
        self._misses = 0
        self._relay = None
        self._guard()
        try:
            self.routes.cleanup(guard=self._guard)
        except RouteError as exc:
            self.log(f"[warning] {exc}")
        self.vpn.disconnect(timeout=config.disconnect_wait_seconds, guard=self._guard)
        self._set(Phase.DISCOVERING, "正在刷新 CIS VPN Gate 节点目录", relay="", country="", vpn_ipv4="", route_count=0)

        try:
            relays = self.catalog()
        except Exception as exc:
            if self.known_good is None or not self.known_good.fresh():
                self._set(Phase.WAITING, f"节点目录读取失败: {exc}")
                return self._backoff()
            self.log(f"[warning] 节点目录读取失败，只尝试近期成功节点: {exc}")
            relays = []
        if self.known_good is not None:
            relays = self.known_good.merge(relays)
        candidates = order_candidates(
            self.cooldown.filter(relays),
            per_country=config.max_candidates_per_country,
            total=config.max_candidates_total,
        )
        if not candidates:
            self._set(Phase.WAITING, "目录里没有 CIS 候选节点")
            return self._backoff()

        failures = 0
        for number, relay in enumerate(candidates, start=1):
            if self._stop is not None and self._stop.is_set():
                return 0
            remaining = deadline - self.clock()
            if remaining <= 0:
                self._set(Phase.WAITING, f"本轮切换已达 {config.failover_timeout_seconds} 秒上限，稍后重试")
                return self._backoff()
            self._guard()
            self._set(Phase.CONNECTING, f"正在连接候选 {number}/{len(candidates)}: {relay.country} {relay.endpoint}")
            probe_timeout = min(config.tcp_probe_timeout_milliseconds / 1000, remaining)
            if relay.transport == "tcp" and not self.vpn.probe_tcp(relay, timeout=probe_timeout):
                failures += 1
                self.cooldown.record_failure(relay)
                self.log(f"[switching] {relay.country} {relay.endpoint} 失败: TCP 端口不可达")
                continue
            try:
                lease = self.vpn.connect(
                    relay, timeout=min(config.connect_timeout_seconds, remaining), guard=self._guard
                )
            except LocalBusyError as exc:
                # A local SoftEther problem says nothing about the relay.
                self._set(Phase.WAITING, f"SoftEther 本机资源忙，稍后重试: {exc}")
                return self._backoff()
            except SoftEtherError as exc:
                failures += 1
                self.cooldown.record_failure(relay)
                self.log(f"[switching] {relay.country} {relay.endpoint} 失败: {exc}")
                continue
            self.cooldown.record_success(relay)
            if self.known_good is not None:
                self.known_good.remember(relay)
            self._relay = relay
            return self._maintain(lease)

        self._set(Phase.WAITING, f"{failures} 个候选节点都没连上，稍后刷新目录重试")
        return self._backoff()

    def _backoff(self) -> float:
        self._failed_rounds += 1
        base = self.config.failed_cycle_retry_seconds
        return min(self.config.failed_cycle_backoff_max_seconds, base * 2 ** min(self._failed_rounds - 1, 8))

    # --- lifecycle ------------------------------------------------------------

    def run(self, stop: StopSignal) -> None:
        self._stop = stop
        self.log("keeper started")
        self._set(Phase.STARTING, "正在启动")
        while not stop.is_set():
            try:
                wait = self.step()
            except Exception as exc:  # keep the process alive; retry later
                self.log(f"[error] 未处理的异常: {exc!r}")
                self._set(Phase.FAILED, f"内部错误: {exc}")
                wait = self._backoff()
            stop.wait(max(1.0, wait))
        self.shutdown()

    def shutdown(self) -> None:
        """Explicit stop: remove owned routes and disconnect, even mid-match."""

        self._set(Phase.STOPPING, "正在撤销临时路由并断开 VPN")
        errors = []
        try:
            self.routes.cleanup()
        except RouteError as exc:
            errors.append(str(exc))
        try:
            if not self.vpn.disconnect(timeout=self.config.disconnect_wait_seconds):
                errors.append("SoftEther 会话没有在时限内断开")
        except SoftEtherError as exc:
            errors.append(str(exc))
        self._ready = False
        if errors:
            self._set(Phase.FAILED, "停止时清理不完整: " + "; ".join(errors))
        else:
            self._set(Phase.STOPPED, "已停止", relay="", country="", vpn_ipv4="", route_count=0)
