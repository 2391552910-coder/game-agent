from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


class RecoveryConsistencyError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(f"gateway v2 recovery data is inconsistent: {detail}")


def _required_string(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RecoveryConsistencyError(f"invalid {field}")
    return value


def _required_generation(row: Mapping[str, Any]) -> int:
    value = row.get("control_generation")
    if type(value) is not int or value <= 0:
        raise RecoveryConsistencyError("invalid control_generation")
    return value


class EventFingerprintRegistry:
    def __init__(self, fingerprints: Mapping[tuple[str, str], str] | None = None) -> None:
        owned: dict[tuple[str, str], str] = {}
        for key, content_hash in (fingerprints or {}).items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or any(not isinstance(part, str) or not part.strip() for part in key)
                or not isinstance(content_hash, str)
                or not content_hash.strip()
            ):
                raise RecoveryConsistencyError("invalid event fingerprint")
            owned[key] = content_hash
        self._fingerprints = MappingProxyType(owned)

    def is_seen(self, gateway_id: str, event_id: str) -> bool:
        return (gateway_id, event_id) in self._fingerprints

    def matches(self, gateway_id: str, event_id: str, content_hash: str) -> bool:
        return self._fingerprints.get((gateway_id, event_id)) == content_hash

    def content_hash_for(self, gateway_id: str, event_id: str) -> str | None:
        return self._fingerprints.get((gateway_id, event_id))


@dataclass(frozen=True)
class RecoveredEvent:
    gateway_id: str
    event_id: str
    content_hash: str
    event_type: str
    session_id: str
    control_generation: int
    status: str


@dataclass(frozen=True)
class RecoveredDecision:
    gateway_id: str
    decision_id: str
    session_id: str
    control_generation: int
    status: str


@dataclass(frozen=True)
class RecoveredTerminal:
    gateway_id: str
    skill_call_id: str
    decision_id: str
    session_id: str
    terminal_event_id: str | None
    status: str


@dataclass(frozen=True)
class RecoveryProjection:
    fingerprints: EventFingerprintRegistry
    events_by_id: Mapping[str, RecoveredEvent]
    decisions_by_id: Mapping[str, RecoveredDecision]
    terminal_by_skill_call: Mapping[str, RecoveredTerminal]
    decision_ids: tuple[str, ...]

    @classmethod
    def from_rows(
        cls,
        *,
        event_rows: Iterable[Mapping[str, Any]],
        decision_rows: Iterable[Mapping[str, Any]],
        skill_call_rows: Iterable[Mapping[str, Any]],
    ) -> RecoveryProjection:
        fingerprints: dict[tuple[str, str], str] = {}
        events: dict[str, RecoveredEvent] = {}
        for row in event_rows:
            event = RecoveredEvent(
                gateway_id=_required_string(row, "gateway_id"),
                event_id=_required_string(row, "event_id"),
                content_hash=_required_string(row, "content_hash"),
                event_type=_required_string(row, "event_type"),
                session_id=_required_string(row, "session_id"),
                control_generation=_required_generation(row),
                status=_required_string(row, "status"),
            )
            key = (event.gateway_id, event.event_id)
            existing_hash = fingerprints.get(key)
            if existing_hash is not None and existing_hash != event.content_hash:
                raise RecoveryConsistencyError(f"conflicting event fingerprint {event.event_id}")
            projection_key = f"{event.gateway_id}:{event.event_id}"
            existing_event = events.get(projection_key)
            if existing_event is not None and existing_event != event:
                raise RecoveryConsistencyError(f"conflicting event {event.event_id}")
            fingerprints[key] = event.content_hash
            events[projection_key] = event

        decisions: dict[str, RecoveredDecision] = {}
        for row in decision_rows:
            decision = RecoveredDecision(
                gateway_id=_required_string(row, "gateway_id"),
                decision_id=_required_string(row, "decision_id"),
                session_id=_required_string(row, "session_id"),
                control_generation=_required_generation(row),
                status=_required_string(row, "status"),
            )
            projection_key = f"{decision.gateway_id}:{decision.decision_id}"
            existing_decision = decisions.get(projection_key)
            if existing_decision is not None and existing_decision != decision:
                raise RecoveryConsistencyError(f"conflicting decision {decision.decision_id}")
            decisions[projection_key] = decision

        terminals: dict[str, RecoveredTerminal] = {}
        for row in skill_call_rows:
            terminal_event_id_value = row.get("terminal_event_id")
            if terminal_event_id_value is not None and (
                not isinstance(terminal_event_id_value, str) or not terminal_event_id_value.strip()
            ):
                raise RecoveryConsistencyError("invalid terminal_event_id")
            terminal = RecoveredTerminal(
                gateway_id=_required_string(row, "gateway_id"),
                skill_call_id=_required_string(row, "skill_call_id"),
                decision_id=_required_string(row, "decision_id"),
                session_id=_required_string(row, "session_id"),
                terminal_event_id=terminal_event_id_value,
                status=_required_string(row, "status"),
            )
            if terminal.status in {"pending", "started"}:
                if terminal.terminal_event_id is not None:
                    raise RecoveryConsistencyError(
                        f"non-terminal skill call has terminal event {terminal.terminal_event_id}"
                    )
            else:
                if terminal.terminal_event_id is None:
                    raise RecoveryConsistencyError(f"missing terminal event for {terminal.skill_call_id}")
                event = events.get(f"{terminal.gateway_id}:{terminal.terminal_event_id}")
                if event is None:
                    raise RecoveryConsistencyError(f"missing terminal event {terminal.terminal_event_id}")
                if event.event_type != "skill_finished" or event.session_id != terminal.session_id:
                    raise RecoveryConsistencyError(f"invalid terminal event {terminal.terminal_event_id}")
            decision = decisions.get(f"{terminal.gateway_id}:{terminal.decision_id}")
            if decision is None:
                raise RecoveryConsistencyError(f"missing decision {terminal.decision_id}")
            if decision.session_id != terminal.session_id:
                raise RecoveryConsistencyError(f"terminal decision mismatch {terminal.decision_id}")
            projection_key = f"{terminal.gateway_id}:{terminal.skill_call_id}"
            existing_terminal = terminals.get(projection_key)
            if existing_terminal is not None and existing_terminal != terminal:
                raise RecoveryConsistencyError(f"conflicting skill call {terminal.skill_call_id}")
            terminals[projection_key] = terminal

        return cls(
            fingerprints=EventFingerprintRegistry(fingerprints),
            events_by_id=MappingProxyType(events),
            decisions_by_id=MappingProxyType(decisions),
            terminal_by_skill_call=MappingProxyType(terminals),
            decision_ids=tuple(sorted(decision.decision_id for decision in decisions.values())),
        )
