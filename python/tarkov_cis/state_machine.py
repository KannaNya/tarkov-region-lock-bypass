"""An explicit, side-effect-free connection lifecycle state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .models import ConnectionPhase, Relay


class ConnectionEvent(str, Enum):
    START = "start"
    CANDIDATE_SELECTED = "candidate_selected"
    TCP_CONNECTED = "tcp_connected"
    SESSION_ESTABLISHED = "session_established"
    SESSION_RESTORED = "session_restored"
    ROUTES_APPLIED = "routes_applied"
    ATTEMPT_FAILED = "attempt_failed"
    NO_CANDIDATES = "no_candidates"
    RETRY_READY = "retry_ready"
    DISCOVERY_FAILED = "discovery_failed"
    ROUTES_FAILED = "routes_failed"
    CONNECTION_LOST = "connection_lost"
    DISCONNECT_REQUESTED = "disconnect_requested"
    DISCONNECTED = "disconnected"
    RESET = "reset"


class InvalidTransition(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Transition:
    occurred_at: datetime
    event: ConnectionEvent
    previous: ConnectionPhase
    current: ConnectionPhase
    endpoint: str | None = None
    reason: str = ""


class ConnectionStateMachine:
    """Validate lifecycle transitions while all I/O remains in adapter classes."""

    _STATIC_TRANSITIONS = {
        (ConnectionPhase.DISCONNECTED, ConnectionEvent.START): ConnectionPhase.DISCOVERING,
        (ConnectionPhase.DISCOVERING, ConnectionEvent.CANDIDATE_SELECTED): ConnectionPhase.CONNECTING,
        (ConnectionPhase.DISCOVERING, ConnectionEvent.NO_CANDIDATES): ConnectionPhase.COOLING,
        (ConnectionPhase.DISCOVERING, ConnectionEvent.DISCOVERY_FAILED): ConnectionPhase.FAILED,
        (ConnectionPhase.CONNECTING, ConnectionEvent.TCP_CONNECTED): ConnectionPhase.VERIFYING_SESSION,
        (ConnectionPhase.CONNECTING, ConnectionEvent.ATTEMPT_FAILED): ConnectionPhase.DISCOVERING,
        (ConnectionPhase.VERIFYING_SESSION, ConnectionEvent.SESSION_ESTABLISHED): ConnectionPhase.APPLYING_ROUTES,
        (ConnectionPhase.VERIFYING_SESSION, ConnectionEvent.ATTEMPT_FAILED): ConnectionPhase.DISCOVERING,
        (ConnectionPhase.APPLYING_ROUTES, ConnectionEvent.ROUTES_APPLIED): ConnectionPhase.READY,
        (ConnectionPhase.APPLYING_ROUTES, ConnectionEvent.ROUTES_FAILED): ConnectionPhase.FAILED,
        (ConnectionPhase.READY, ConnectionEvent.CONNECTION_LOST): ConnectionPhase.DISCOVERING,
        (ConnectionPhase.READY, ConnectionEvent.ROUTES_FAILED): ConnectionPhase.FAILED,
        (ConnectionPhase.DISCONNECTED, ConnectionEvent.SESSION_RESTORED): ConnectionPhase.APPLYING_ROUTES,
        (ConnectionPhase.DISCOVERING, ConnectionEvent.SESSION_RESTORED): ConnectionPhase.APPLYING_ROUTES,
        (ConnectionPhase.COOLING, ConnectionEvent.SESSION_RESTORED): ConnectionPhase.APPLYING_ROUTES,
        (ConnectionPhase.FAILED, ConnectionEvent.SESSION_RESTORED): ConnectionPhase.APPLYING_ROUTES,
        (ConnectionPhase.COOLING, ConnectionEvent.RETRY_READY): ConnectionPhase.DISCOVERING,
        (ConnectionPhase.FAILED, ConnectionEvent.RESET): ConnectionPhase.DISCONNECTED,
        (ConnectionPhase.DISCONNECTING, ConnectionEvent.DISCONNECTED): ConnectionPhase.DISCONNECTED,
    }
    _DISCONNECTABLE = frozenset(
        phase for phase in ConnectionPhase if phase not in {ConnectionPhase.DISCONNECTED, ConnectionPhase.DISCONNECTING}
    )

    def __init__(self, phase: ConnectionPhase = ConnectionPhase.DISCONNECTED) -> None:
        self.phase = phase
        self.candidate: Relay | None = None
        self.failure_reason = ""
        self._history: list[Transition] = []

    @classmethod
    def initial(cls) -> "ConnectionStateMachine":
        return cls(ConnectionPhase.DISCONNECTED)

    @property
    def history(self) -> tuple[Transition, ...]:
        return tuple(self._history)

    def transition(
        self,
        event: ConnectionEvent,
        *,
        candidate: Relay | None = None,
        reason: str = "",
        occurred_at: datetime | None = None,
    ) -> ConnectionPhase:
        if not isinstance(event, ConnectionEvent):
            raise TypeError("event must be a ConnectionEvent")
        if event is ConnectionEvent.DISCONNECT_REQUESTED and self.phase in self._DISCONNECTABLE:
            target = ConnectionPhase.DISCONNECTING
        else:
            target = self._STATIC_TRANSITIONS.get((self.phase, event))
        if target is None:
            raise InvalidTransition(f"{event.value} is invalid while {self.phase.value}")
        if event is ConnectionEvent.CANDIDATE_SELECTED and candidate is None:
            raise ValueError("candidate_selected requires a Relay")

        previous = self.phase
        endpoint = candidate.endpoint if candidate is not None else (
            self.candidate.endpoint if self.candidate is not None else None
        )
        self.phase = target
        if event is ConnectionEvent.CANDIDATE_SELECTED:
            self.candidate = candidate
            self.failure_reason = ""
        elif event in {
            ConnectionEvent.ATTEMPT_FAILED,
            ConnectionEvent.DISCOVERY_FAILED,
            ConnectionEvent.ROUTES_FAILED,
            ConnectionEvent.CONNECTION_LOST,
        }:
            self.failure_reason = reason.strip()
            self.candidate = None
        elif target in {ConnectionPhase.DISCONNECTED, ConnectionPhase.COOLING}:
            self.candidate = None
            if target is ConnectionPhase.DISCONNECTED:
                self.failure_reason = ""

        timestamp = occurred_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        self._history.append(
            Transition(
                occurred_at=timestamp,
                event=event,
                previous=previous,
                current=target,
                endpoint=endpoint,
                reason=reason.strip(),
            )
        )
        return self.phase

    def start(self) -> ConnectionPhase:
        return self.transition(ConnectionEvent.START)

    def candidate_selected(self, candidate: Relay) -> ConnectionPhase:
        return self.transition(ConnectionEvent.CANDIDATE_SELECTED, candidate=candidate)

    def tcp_connected(self) -> ConnectionPhase:
        return self.transition(ConnectionEvent.TCP_CONNECTED)

    def session_established(self) -> ConnectionPhase:
        return self.transition(ConnectionEvent.SESSION_ESTABLISHED)

    def session_restored(self) -> ConnectionPhase:
        return self.transition(ConnectionEvent.SESSION_RESTORED)

    def routes_applied(self) -> ConnectionPhase:
        return self.transition(ConnectionEvent.ROUTES_APPLIED)

    def attempt_failed(self, reason: str) -> ConnectionPhase:
        return self.transition(ConnectionEvent.ATTEMPT_FAILED, reason=reason)

    def no_candidates(self) -> ConnectionPhase:
        return self.transition(ConnectionEvent.NO_CANDIDATES)

    def retry_ready(self) -> ConnectionPhase:
        return self.transition(ConnectionEvent.RETRY_READY)

    def disconnect(self) -> ConnectionPhase:
        return self.transition(ConnectionEvent.DISCONNECT_REQUESTED)

    def disconnected(self) -> ConnectionPhase:
        return self.transition(ConnectionEvent.DISCONNECTED)

    def reset(self) -> ConnectionPhase:
        return self.transition(ConnectionEvent.RESET)
