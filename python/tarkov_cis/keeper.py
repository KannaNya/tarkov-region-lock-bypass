"""Long-running orchestration with explicit, observable connection states."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import threading
import time
from typing import Any, Callable, Iterable

from .eft_logs import discover_authorization_hosts, resolve_authorization_targets
from .game_phase import GamePhase, GamePhaseSnapshot, RaidStarted, PlayProtectionActivated
from .models import ConnectionPhase
from .process_runner import CommandError
from .routing import RouteManager
from .softether import LocalResourceBusyError, SoftEtherClient, SoftEtherError, VpnLease
from .state_machine import ConnectionEvent, ConnectionStateMachine, InvalidTransition


class KeeperPhase(str, Enum):
    STOPPED = "stopped"
    CHECKING = "checking"
    DISCOVERING = "discovering"
    CONNECTING = "connecting"
    APPLYING_ROUTES = "applying_routes"
    READY = "ready"
    PLAY_PROTECTED = "play_protected"
    RAID_DIRECT = "raid_direct"
    SWITCHING = "switching"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class KeeperStatus:
    phase: KeeperPhase = KeeperPhase.STOPPED
    connection_phase: ConnectionPhase = ConnectionPhase.DISCONNECTED
    detail: str = "未启动"
    relay: str = ""
    country: str = ""
    vpn_ipv4: str = ""
    target_count: int = 0
    failed_candidates: int = 0
    game_phase: str = GamePhaseSnapshot().phase.value
    game_evidence: str = ""
    play_protected: bool = False
    updated_at: datetime = datetime.min.replace(tzinfo=timezone.utc)


LogSink = Callable[[str], None]
StatusSink = Callable[[KeeperStatus], None]


class _FailoverDeadlineExpired(RuntimeError):
    """The shared cycle budget expired; this is not remote relay evidence."""


def _relay_endpoint(relay: Any) -> str:
    endpoint = getattr(relay, "endpoint", None)
    if endpoint:
        return str(endpoint)
    host = getattr(relay, "ip", None) or getattr(relay, "host_name", "")
    return f"{host}:{getattr(relay, 'port', '')}".rstrip(":")


class KeeperService:
    """Coordinate discovery, verified SoftEther failover and /32 routes."""

    def __init__(
        self,
        *,
        config: Any,
        candidate_provider: Any,
        softether: SoftEtherClient,
        routes: RouteManager,
        logger: LogSink | None = None,
        status_sink: StatusSink | None = None,
        health_probe: Callable | None = None,
        game_phase_probe: Callable[[], GamePhaseSnapshot] | None = None,
    ) -> None:
        self.config = config
        self.candidate_provider = candidate_provider
        self.softether = softether
        self.routes = routes
        self._logger = logger or (lambda _message: None)
        self._status_sink = status_sink or (lambda _status: None)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status = KeeperStatus()
        self.machine = ConnectionStateMachine.initial()
        self.health_probe = health_probe
        self.game_phase_probe = game_phase_probe
        self._health_failures = 0
        self._session_failures = 0
        self._last_verified_lease: VpnLease | None = None
        self._last_targets: tuple[Any, ...] = ()
        self._last_identity = None
        self._health_switch = False
        self._active_relay = None
        self._last_game_phase = GamePhaseSnapshot()
        self._raid_direct_session: str | None = None
        self._explicit_cleanup = False
        # Guard individual adapter commands too: connect()/sync() may perform
        # several destructive operations after a slow preceding command.
        for adapter in (self.softether, self.routes):
            if isinstance(adapter, (SoftEtherClient, RouteManager)):
                adapter.operation_guard = self._check_play_protection

    @property
    def status(self) -> KeeperStatus:
        with self._lock:
            return replace(self._status)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _set_status(self, phase: KeeperPhase, detail: str, **updates: Any) -> None:
        with self._lock:
            self._status = replace(
                self._status,
                phase=phase,
                connection_phase=self.machine.phase,
                detail=detail,
                updated_at=datetime.now(timezone.utc),
                **updates,
            )
            snapshot = replace(self._status)
        self._logger(f"[{phase.value}] {detail}")
        try:
            self._status_sink(snapshot)
        except Exception as exc:
            self._logger(f"[warning] 无法写入状态快照: {exc}")

    def _prepare_discovery(self) -> None:
        """Normalize the strict core lifecycle before a fresh catalog cycle."""

        phase = self.machine.phase
        if phase is ConnectionPhase.READY:
            self.machine.transition(
                ConnectionEvent.CONNECTION_LOST,
                reason="verified SoftEther session is no longer available",
            )
        elif phase is ConnectionPhase.COOLING:
            self.machine.retry_ready()
        elif phase is ConnectionPhase.FAILED:
            self.machine.reset()
            self.machine.start()
        elif phase is ConnectionPhase.DISCONNECTED:
            self.machine.start()
        elif phase is ConnectionPhase.DISCONNECTING:
            self.machine.disconnected()
            self.machine.start()
        elif phase in {
            ConnectionPhase.CONNECTING,
            ConnectionPhase.VERIFYING_SESSION,
            ConnectionPhase.APPLYING_ROUTES,
        }:
            # A prior synchronous cycle cannot legitimately leave one of these
            # phases behind.  Reset it explicitly after the best-effort I/O
            # cleanup, rather than silently accepting an impossible state.
            self.machine.disconnect()
            self.machine.disconnected()
            self.machine.start()
        elif phase is not ConnectionPhase.DISCOVERING:
            raise RuntimeError(f"unsupported connection phase: {phase.value}")

    def _restore_session_phase(self) -> None:
        if self.machine.phase in {ConnectionPhase.READY, ConnectionPhase.APPLYING_ROUTES}:
            return
        # A play guard may suspend an in-flight connection before its state
        # transitions finish.  A fresh verified lease can resume those states.
        if self.machine.phase is ConnectionPhase.CONNECTING:
            self.machine.tcp_connected()
        if self.machine.phase is ConnectionPhase.VERIFYING_SESSION:
            self.machine.session_established()
            return
        if self.machine.phase in {
            ConnectionPhase.DISCONNECTED,
            ConnectionPhase.DISCOVERING,
            ConnectionPhase.COOLING,
            ConnectionPhase.FAILED,
        }:
            self.machine.session_restored()
            return
        if self.machine.phase is ConnectionPhase.DISCONNECTING:
            self.machine.disconnected()
            self.machine.session_restored()
            return
        raise InvalidTransition(
            f"verified session cannot be restored while {self.machine.phase.value}"
        )

    def _candidates(self) -> list[Any]:
        provider = self.candidate_provider
        if callable(provider):
            return list(provider())
        for method_name in ("candidates", "get_candidates", "discover"):
            method = getattr(provider, method_name, None)
            if callable(method):
                return list(method())
        raise TypeError("candidate provider must be callable or expose candidates()/discover()")

    def _record_failure(self, relay: Any, reason: str) -> None:
        provider = self.candidate_provider
        method = getattr(provider, "record_failure", None)
        if callable(method):
            try:
                method(relay, reason)
            except Exception as exc:
                self._logger(f"[warning] 无法记录节点失败状态: {exc}")

    def _record_success(self, relay: Any) -> None:
        provider = self.candidate_provider
        method = getattr(provider, "record_success", None)
        if callable(method):
            try:
                method(relay)
            except Exception as exc:
                self._logger(f"[warning] 无法记录近期成功节点: {exc}")

    def _disconnect_timeout(self) -> float:
        return max(1.0, float(getattr(self.config, "disconnect_wait_seconds", 15)))

    def _health_failure_limit(self) -> int:
        return max(1, int(getattr(self.config, "health_failure_threshold", 3)))

    def _session_failure_limit(self) -> int:
        return max(1, int(getattr(self.config, "session_failure_threshold", 3)))

    def _failed_cycle_backoff_max(self) -> int:
        base = max(1, int(getattr(self.config, "failed_cycle_retry_seconds", 10)))
        return max(base, int(getattr(self.config, "failed_cycle_backoff_max_seconds", 120)))

    def _game_phase(self) -> GamePhaseSnapshot:
        probe = self.game_phase_probe
        if probe is None:
            return GamePhaseSnapshot()
        try:
            snapshot = probe()
        except Exception as exc:
            self._logger(f"[warning] 游戏阶段读取失败：{exc}")
            snapshot = GamePhaseSnapshot(detail=f"阶段读取失败：{exc}", process_running=None)
        if not isinstance(snapshot, GamePhaseSnapshot):
            self._logger("[warning] 游戏阶段探测器返回了无效结果，按未知阶段处理")
            snapshot = GamePhaseSnapshot(detail="阶段探测器返回无效结果", process_running=None)
        previous = self._last_game_phase
        same_session = not (
            previous.session_id and snapshot.session_id
            and previous.session_id != snapshot.session_id
        )
        if (
            same_session
            and previous.phase in {GamePhase.MATCHMAKING, GamePhase.RAID, GamePhase.RAID_STARTED}
            and previous.process_running is not False
            and snapshot.process_running is not False
            and (
                snapshot.phase is GamePhase.UNKNOWN
                or (
                    previous.observed_at is not None
                    and snapshot.observed_at is not None
                    and snapshot.observed_at < previous.observed_at
                )
            )
        ):
            # Bounded tails can lose the start marker; an unreadable log or
            # process query must not silently unlock a known active Raid.
            snapshot = replace(previous, process_running=snapshot.process_running)
        self._last_game_phase = snapshot
        return snapshot

    def _check_play_protection(self) -> None:
        if self._explicit_cleanup:
            return
        snapshot = self._game_phase()
        if self._disconnect_at_raid() and self._raid_has_started(snapshot):
            raise RaidStarted()
        if self._protect_active_raid(snapshot):
            raise PlayProtectionActivated()

    def _disconnect_at_raid(self) -> bool:
        return bool(getattr(self.config, "disconnect_at_raid", False))

    def _raid_has_started(self, snapshot: GamePhaseSnapshot) -> bool:
        if snapshot.process_running is False:
            self._raid_direct_session = None
            return False
        if (self._raid_direct_session not in (None, "current-game") and snapshot.session_id
                and snapshot.session_id != self._raid_direct_session):
            self._raid_direct_session = None
        return self._raid_direct_session is not None or (
            snapshot.process_running is True
            and (snapshot.phase is GamePhase.RAID_STARTED or snapshot.raid_started_seen)
        )

    def _enter_raid_direct(self, snapshot: GamePhaseSnapshot) -> bool:
        if self._raid_direct_session is not None:
            return True
        errors: list[str] = []
        self._explicit_cleanup = True
        try:
            try:
                self.routes.cleanup()
            except Exception as exc:
                errors.append(f"临时路由: {exc}")
            try:
                if not self.softether.disconnect(timeout=self._disconnect_timeout()):
                    errors.append("SoftEther 会话未确认断开")
            except Exception as exc:
                errors.append(f"SoftEther: {exc}")
        finally:
            self._explicit_cleanup = False
        if errors:
            self._set_status(KeeperPhase.FAILED, "进入 Raid 后清理失败: " + "; ".join(errors),
                             game_phase=snapshot.phase.value, game_evidence=snapshot.detail)
            return False
        if self.machine.phase is not ConnectionPhase.DISCONNECTED:
            if self.machine.phase is not ConnectionPhase.DISCONNECTING:
                self.machine.disconnect()
            self.machine.disconnected()
        self._last_verified_lease = None
        self._active_relay = None
        self._last_targets = ()
        self._last_identity = None
        self._health_failures = 0
        self._session_failures = 0
        self._health_switch = False
        self._raid_direct_session = snapshot.session_id or "current-game"
        self._set_status(KeeperPhase.RAID_DIRECT, "已进入 Raid；鉴权路由已撤销，VPN 已断开",
                         game_phase=snapshot.phase.value, game_evidence=snapshot.detail,
                         play_protected=False, relay="", country="", vpn_ipv4="", target_count=0)
        return True

    def _raid_protection_enabled(self) -> bool:
        return bool(getattr(self.config, "pause_during_raid", True))

    def _protect_active_raid(self, snapshot: GamePhaseSnapshot) -> bool:
        """Keep an established relay stable while the game process runs."""

        protected_phases = {GamePhase.MATCHMAKING, GamePhase.RAID, GamePhase.RAID_STARTED, GamePhase.POST_RAID}
        game_running_with_relay = (
            snapshot.process_running is True and self.machine.phase is ConnectionPhase.READY
        )
        if not self._raid_protection_enabled() or (
            snapshot.phase not in protected_phases and not game_running_with_relay
        ):
            return False
        # A stale marker from an old log must not freeze a fresh login.  The
        # parser only marks protection active while the game process exists.
        if snapshot.process_running is False:
            return False
        # Pre-Raid misses do not count as consecutive failures after a match.
        self._health_failures = 0
        self._session_failures = 0
        self._health_switch = False
        current = self.status
        label = "Raid" if snapshot.phase in {GamePhase.RAID, GamePhase.RAID_STARTED} else "游戏"
        self._set_status(
            KeeperPhase.PLAY_PROTECTED,
            f"{label}保护中：暂停节点切换（{snapshot.detail}）",
            game_phase=snapshot.phase.value,
            game_evidence=snapshot.detail,
            play_protected=True,
            relay=current.relay,
            country=current.country,
            vpn_ipv4=current.vpn_ipv4,
        )
        return True

    def _best_effort_reset(self) -> None:
        self._check_play_protection()
        try:
            self.routes.cleanup()
        except PlayProtectionActivated:
            raise
        except Exception as exc:
            self._logger(f"[warning] 临时路由清理失败: {exc}")
        self._check_play_protection()
        try:
            self.softether.disconnect(timeout=self._disconnect_timeout())
        except PlayProtectionActivated:
            raise
        except Exception as exc:
            self._logger(f"[warning] SoftEther 会话清理失败: {exc}")

    def _authorization_targets(self) -> tuple[Any, ...]:
        from .eft_logs import discover_log_roots
        roots = discover_log_roots(getattr(self.config, "game_log_roots", ()) or ())
        from .eft_logs import DEFAULT_AUTHORIZATION_HOSTS
        static_hosts = tuple(getattr(self.config, "target_hosts", ()) or ()) + DEFAULT_AUTHORIZATION_HOSTS
        hosts = discover_authorization_hosts(roots, static_hosts=static_hosts)
        return resolve_authorization_targets(hosts)

    def _sync_routes(
        self,
        lease: VpnLease,
        relay: Any | None = None,
        *,
        deadline: float | None = None,
    ) -> bool:
        self._check_play_protection()
        self._restore_session_phase()
        try:
            targets = self._authorization_targets()
        except Exception as exc:
            self._logger(f"[warning] DNS resolution failed: {exc}")
            targets = ()
        self._check_play_protection()
        identity = (lease.interface_index, lease.gateway)
        if not targets:
            # Never interpret transient DNS failure as an instruction to delete
            # healthy /32 routes. Do not carry a lease's routes onto another NIC.
            if self._last_targets and identity == self._last_identity:
                self._set_status(KeeperPhase.RETRY_WAIT, "DNS 暂无结果；保留上一组鉴权 /32，稍后重试")
                return False
            self.machine.transition(
                ConnectionEvent.ROUTES_FAILED,
                reason="no authorization targets resolved",
            )
            try:
                self.routes.cleanup()
            except PlayProtectionActivated:
                raise
            except Exception as exc:
                self._set_status(
                    KeeperPhase.FAILED,
                    f"没有鉴权目标，且旧临时路由清理失败: {exc}",
                )
                raise
            self._set_status(
                KeeperPhase.FAILED,
                "没有解析到任何 lobby/WSN/gw-pvp 鉴权目标；未添加路由",
            )
            return False
        self._set_status(KeeperPhase.APPLYING_ROUTES, "正在同步临时 /32 鉴权路由")
        try:
            self.routes.sync(
                (target.ip for target in targets),
                lease,
                deadline=deadline,
            )
        except PlayProtectionActivated:
            raise
        except Exception as exc:
            self._check_play_protection()
            if self.machine.phase in {ConnectionPhase.APPLYING_ROUTES, ConnectionPhase.READY}:
                self.machine.transition(ConnectionEvent.ROUTES_FAILED, reason=str(exc))
            raise
        if self.machine.phase is ConnectionPhase.APPLYING_ROUTES:
            self.machine.routes_applied()
        self._last_targets, self._last_identity = targets, identity
        if self.health_probe is not None:
            self._check_play_protection()
            health = self.health_probe(targets, deadline=deadline)
            self._check_play_protection()
            if not health.ok:
                self._health_failures += 1
                limit = self._health_failure_limit()
                self._health_switch = self._health_failures >= limit
                self._set_status(KeeperPhase.RETRY_WAIT,
                                 f"HTTPS 响应探测失败 {self._health_failures}/{limit}: {health.detail}；保留当前会话")
                return False
            self._health_failures = 0
            self._health_switch = False
        if relay is not None:
            self._record_success(relay)
        self._set_status(
            KeeperPhase.READY,
            "CIS 会话和鉴权路由已就绪",
            relay=_relay_endpoint(relay) if relay is not None else self.status.relay,
            country=str(getattr(relay, "country_short", "")) if relay is not None else self.status.country,
            vpn_ipv4=lease.ipv4,
            target_count=len(targets),
        )
        return True

    def run_cycle(self) -> bool:
        """Run one health/reconciliation cycle; switch after relay failures."""

        try:
            return self._run_cycle()
        except RaidStarted:
            return self._enter_raid_direct(self._game_phase())
        except PlayProtectionActivated:
            # This is a successful pause, not a relay failure.  In particular,
            # do not route it through reset()/disconnect() exception handlers.
            return True

    def _run_cycle(self) -> bool:

        snapshot = self._game_phase()
        self._last_game_phase = snapshot
        if self._disconnect_at_raid() and self._raid_has_started(snapshot):
            return self._enter_raid_direct(snapshot)
        if self._protect_active_raid(snapshot):
            # Do not call verified_connection(), DNS, HTTPS health probes,
            # route cleanup, disconnect(), or candidate discovery here.  The
            # current /32 routes and SoftEther session stay untouched through
            # matching, Raid, and post-Raid settlement.
            return True

        skip_endpoint = ""
        failover_deadline = time.monotonic() + max(
            1, int(getattr(self.config, "failover_timeout_seconds", 180))
        )
        self._set_status(
            KeeperPhase.CHECKING,
            "正在检查 SoftEther 会话和 VPN 网卡",
            game_phase=snapshot.phase.value,
            game_evidence=snapshot.detail,
            play_protected=False,
        )
        try:
            lease = self.softether.verified_connection()
        except PlayProtectionActivated:
            raise
        except Exception as exc:
            self._logger(f"[warning] 会话检查失败: {exc}")
            lease = None
        self._check_play_protection()
        if lease is None:
            if self._last_verified_lease is not None:
                self._session_failures += 1
                limit = self._session_failure_limit()
                if self._session_failures < limit:
                    self._set_status(
                        KeeperPhase.RETRY_WAIT,
                        f"SoftEther 会话/租约暂时不可读 {self._session_failures}/{limit}；不重置虚拟网卡",
                        vpn_ipv4=self._last_verified_lease.ipv4,
                    )
                    return False
                self._logger(
                    f"[switching] SoftEther 会话/租约连续 {self._session_failures} 次不可读，开始有界节点切换"
                )
                self._last_verified_lease = None
                self._session_failures = 0
            # No previously verified session exists; initial discovery is safe.
        else:
            # A single successful verification clears only the session-loss
            # counter.  Health failures are tracked independently below.
            self._session_failures = 0
            self._last_verified_lease = lease
        if lease is not None:
            try:
                ready = self._sync_routes(lease, deadline=failover_deadline)
                if ready or not self._health_switch:
                    return ready
                self._health_switch = False
                self._health_failures = 0
                skip_endpoint = _relay_endpoint(self._active_relay) if self._active_relay is not None else ""
                self._set_status(
                    KeeperPhase.SWITCHING,
                    f"连续 {self._health_failure_limit()} 次 HTTPS 响应失败，开始有界节点切换",
                )
            except PlayProtectionActivated:
                raise
            except Exception as exc:
                self._set_status(KeeperPhase.FAILED, f"鉴权路由同步失败: {exc}")
                return False

        self._check_play_protection()
        try:
            self.routes.cleanup()
        except PlayProtectionActivated:
            raise
        except Exception as exc:
            self._check_play_protection()
            self._set_status(KeeperPhase.FAILED, f"旧的临时路由无法安全清理: {exc}")
            return False

        # Release a half-open local session before consulting a fresh catalog.
        self._check_play_protection()
        try:
            self.softether.disconnect(timeout=self._disconnect_timeout())
        except PlayProtectionActivated:
            raise
        except Exception as exc:
            self._logger(f"[warning] 连接前会话释放不完整: {exc}")
        self._last_verified_lease = None
        self._session_failures = 0
        self._health_failures = 0
        self._health_switch = False

        try:
            self._prepare_discovery()
        except Exception as exc:
            self._set_status(KeeperPhase.FAILED, f"连接状态无法安全复位: {exc}")
            return False

        self._set_status(KeeperPhase.DISCOVERING, "正在刷新可用 CIS VPN Gate 节点")
        self._check_play_protection()
        try:
            candidates = [relay for relay in self._candidates()
                          if not skip_endpoint or _relay_endpoint(relay) != skip_endpoint]
            candidates = candidates[:max(1, int(getattr(self.config, "max_candidates_total", 20)))]
        except Exception as exc:
            if self.machine.phase is ConnectionPhase.DISCOVERING:
                self.machine.transition(ConnectionEvent.DISCOVERY_FAILED, reason=str(exc))
            self._set_status(KeeperPhase.RETRY_WAIT, f"节点目录读取失败: {exc}")
            return False
        self._check_play_protection()
        if not candidates:
            if self.machine.phase is ConnectionPhase.DISCOVERING:
                self.machine.no_candidates()
            self._set_status(KeeperPhase.RETRY_WAIT, "当前没有候选 CIS 节点")
            return False
        if time.monotonic() >= failover_deadline:
            if self.machine.phase is ConnectionPhase.DISCOVERING:
                self.machine.transition(
                    ConnectionEvent.DISCOVERY_FAILED,
                    reason="failover deadline exhausted during discovery",
                )
            self._set_status(KeeperPhase.RETRY_WAIT, "节点发现已耗尽本轮故障转移时限")
            return False

        failures = 0
        for index, relay in enumerate(candidates, start=1):
            if self._stop_event.is_set():
                return False
            self._check_play_protection()
            endpoint = _relay_endpoint(relay)
            country = str(getattr(relay, "country_short", ""))
            remaining = failover_deadline - time.monotonic()
            if remaining <= 0:
                self._best_effort_reset()
                if self.machine.phase is ConnectionPhase.DISCOVERING:
                    self.machine.transition(
                        ConnectionEvent.DISCOVERY_FAILED,
                        reason="shared failover deadline exhausted",
                    )
                self._set_status(
                    KeeperPhase.RETRY_WAIT,
                    "本轮故障转移达到共享时限，稍后刷新目录重试",
                    failed_candidates=failures,
                )
                return False
            self.machine.candidate_selected(relay)
            self._set_status(
                KeeperPhase.CONNECTING,
                f"正在连接候选 {index}/{len(candidates)}: {country} {endpoint}",
                relay=endpoint,
                country=country,
                failed_candidates=failures,
            )
            busy_retries = 0
            try:
                probe_timeout = min(
                    max(0.1, float(getattr(self.config, "tcp_probe_timeout_milliseconds", 1500)) / 1000),
                    max(0.1, failover_deadline - time.monotonic()),
                )
                if str(getattr(relay, "transport", "tcp")) != "udp" and not self.softether.probe_tcp(relay, timeout=probe_timeout):
                    if time.monotonic() >= failover_deadline:
                        raise _FailoverDeadlineExpired(
                            "本轮故障转移共享时限在 TCP 探测期间耗尽"
                        )
                    raise SoftEtherError("节点 TCP 端口不可达")
                self.machine.tcp_connected()
                while True:
                    self._check_play_protection()
                    remaining = failover_deadline - time.monotonic()
                    if remaining <= 0:
                        raise _FailoverDeadlineExpired("本轮故障转移共享时限已耗尽")
                    try:
                        lease = self.softether.connect(
                            relay,
                            timeout=min(
                                float(getattr(self.config, "connect_timeout_seconds", 18)),
                                remaining,
                            ),
                        )
                        if time.monotonic() >= failover_deadline:
                            raise _FailoverDeadlineExpired(
                                "本轮故障转移共享时限在会话验证期间耗尽"
                            )
                        break
                    except LocalResourceBusyError as exc:
                        busy_retries += 1
                        self._best_effort_reset()
                        configured_retries = max(
                            0,
                            int(getattr(self.config, "resource_busy_retry_count", 2)),
                        )
                        if busy_retries > configured_retries:
                            if self.machine.phase in {
                                ConnectionPhase.CONNECTING,
                                ConnectionPhase.VERIFYING_SESSION,
                            }:
                                self.machine.attempt_failed(str(exc))
                            self._set_status(
                                KeeperPhase.RETRY_WAIT,
                                f"SoftEther 本机资源持续忙（退出码 43）；未冷却远端节点: {exc}",
                                failed_candidates=failures,
                            )
                            return False
                        self._logger(
                            f"[local_busy] SoftEther 退出码 43，原节点本地重试 "
                            f"{busy_retries}/{configured_retries}"
                        )
                        if self._stop_event.wait(
                            min(0.5, max(0.0, failover_deadline - time.monotonic()))
                        ):
                            return False
                    except SoftEtherError as exc:
                        if time.monotonic() >= failover_deadline:
                            raise _FailoverDeadlineExpired(
                                "本轮故障转移共享时限在会话验证期间耗尽"
                            ) from exc
                        raise
                self.machine.session_established()
                self._active_relay = relay
            except PlayProtectionActivated:
                raise
            except _FailoverDeadlineExpired as exc:
                # A shared local time budget says nothing about whether this
                # volunteer relay is healthy.  Reset the partial session and
                # retry a fresh catalog cycle without poisoning its cooldown.
                self._best_effort_reset()
                if self.machine.phase in {
                    ConnectionPhase.CONNECTING,
                    ConnectionPhase.VERIFYING_SESSION,
                }:
                    self.machine.attempt_failed(str(exc))
                self._set_status(
                    KeeperPhase.RETRY_WAIT,
                    str(exc),
                    failed_candidates=failures,
                )
                return False
            except (SoftEtherError, CommandError) as exc:
                failures += 1
                self._best_effort_reset()
                if self.machine.phase in {
                    ConnectionPhase.CONNECTING,
                    ConnectionPhase.VERIFYING_SESSION,
                }:
                    self.machine.attempt_failed(str(exc))
                self._record_failure(relay, str(exc))
                self._set_status(
                    KeeperPhase.SWITCHING,
                    f"{country} {endpoint} 失败，切换下一个节点: {exc}",
                    failed_candidates=failures,
                )
                continue
            except Exception as exc:
                # Local programming/configuration failures should not poison
                # every remote relay in the catalog.
                self._best_effort_reset()
                if self.machine.phase in {
                    ConnectionPhase.CONNECTING,
                    ConnectionPhase.VERIFYING_SESSION,
                }:
                    self.machine.attempt_failed(str(exc))
                if self.machine.phase is ConnectionPhase.DISCOVERING:
                    self.machine.transition(ConnectionEvent.DISCOVERY_FAILED, reason=str(exc))
                self._set_status(KeeperPhase.FAILED, f"本地连接流程失败: {exc}")
                return False

            try:
                if self._sync_routes(lease, relay, deadline=failover_deadline):
                    return True
                if self.health_probe is not None and self._health_failures:
                    # Keep this session until independent later cycles confirm
                    # failure; do not rotate on one backend response failure.
                    return False
                self._best_effort_reset()
                return False
            except PlayProtectionActivated:
                raise
            except Exception as exc:
                self._best_effort_reset()
                self._set_status(KeeperPhase.FAILED, f"连接后路由同步失败: {exc}")
                return False

        if self.machine.phase is ConnectionPhase.DISCOVERING:
            self.machine.no_candidates()
        self._set_status(
            KeeperPhase.RETRY_WAIT,
            f"{failures} 个候选节点均未建立有效会话，稍后刷新目录重试",
            failed_candidates=failures,
        )
        return False

    def run_forever(self) -> None:
        self._stop_event.clear()
        failed_cycles = 0
        while not self._stop_event.is_set():
            ready = self.run_cycle()
            if ready:
                failed_cycles = 0
                wait_seconds = int(getattr(self.config, "refresh_seconds", 30))
                if self._disconnect_at_raid() and self.status.phase in {
                    KeeperPhase.READY, KeeperPhase.RAID_DIRECT,
                }:
                    wait_seconds = min(wait_seconds, int(getattr(self.config, "disconnected_poll_seconds", 5)))
            elif self.machine.phase is ConnectionPhase.READY and self.status.phase is not KeeperPhase.FAILED:
                # A retained session is not a failed relay. Poll it at the
                # normal interval so a transient probe/lease miss cannot cause
                # a rapid disconnect/reconnect storm or NCSI browser popups.
                failed_cycles = 0
                wait_seconds = int(getattr(self.config, "refresh_seconds", 30))
            else:
                failed_cycles += 1
                base = max(1, int(getattr(self.config, "failed_cycle_retry_seconds", 10)))
                cap = self._failed_cycle_backoff_max()
                wait_seconds = min(cap, base * (2 ** min(failed_cycles - 1, 8)))
            self._stop_event.wait(max(1, wait_seconds))
        self._set_status(KeeperPhase.STOPPED, "已停止")

    def start(self) -> bool:
        if self.running:
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name="TarkovCisKeeper", daemon=False
        )
        self._thread.start()
        return True

    def request_stop(self) -> None:
        self._stop_event.set()

    def stop(
        self,
        *,
        disconnect: bool = True,
        timeout: float = 5.0,
        disconnect_timeout: float | None = None,
    ) -> None:
        stop_deadline = time.monotonic() + max(0.0, float(timeout))
        self._set_status(KeeperPhase.STOPPING, "正在停止并撤销本进程管理的临时路由")
        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(max(0.0, stop_deadline - time.monotonic()))
            if self._thread.is_alive():
                raise RuntimeError(
                    "keeper is still finishing a bounded connection operation; cleanup was not raced"
                )
        if self.machine.phase not in {
            ConnectionPhase.DISCONNECTED,
            ConnectionPhase.DISCONNECTING,
        }:
            self.machine.disconnect()
        # An explicit user stop is still allowed to release the owned session;
        # only autonomous maintenance is suspended by the play guard.
        self._explicit_cleanup = True
        try:
            try:
                self.routes.cleanup(deadline=stop_deadline)
            finally:
                if disconnect:
                    remaining = stop_deadline - time.monotonic()
                    requested_disconnect_timeout = (
                        remaining
                        if disconnect_timeout is None
                        else min(remaining, max(0.0, float(disconnect_timeout)))
                    )
                    if requested_disconnect_timeout < 0.1:
                        raise RuntimeError(
                            "keeper stop deadline expired before SoftEther disconnect"
                        )
                    self.softether.disconnect(
                        timeout=requested_disconnect_timeout
                    )
        finally:
            self._explicit_cleanup = False
        if self.machine.phase is ConnectionPhase.DISCONNECTING:
            self.machine.disconnected()
        self._set_status(KeeperPhase.STOPPED, "已停止")
