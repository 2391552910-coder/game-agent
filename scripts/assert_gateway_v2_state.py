from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from scripts.v2_e2e_common import open_verified_test_engine, require_test_database_url

_RUNTIME_TABLES = (
    "llm_gateway_sessions",
    "llm_gateway_control_cycles",
    "llm_gateway_events",
    "llm_gateway_decisions",
    "llm_gateway_skill_calls",
)
_FORBIDDEN_EVIDENCE_KEYS = {
    "appSecret",
    "secret",
    "requestBody",
    "rawBody",
    "request_body_bytes",
    "request_body_json",
    "newContext",
    "latestDecisionContext",
}


@dataclass(frozen=True)
class SessionEvidence:
    session_id: str
    gateway_id: str
    control_generation: int
    event_ids_by_type: Mapping[str, tuple[str, ...]]
    decision_ids: tuple[str, str]
    skill_call_ids: tuple[str, ...]
    metrics_before: Mapping[str, int]
    metrics_after: Mapping[str, int]


@dataclass(frozen=True)
class GapProbe:
    control_generation: int
    event_ids: tuple[str, ...]


@dataclass(frozen=True)
class OldGenerationProbe:
    old_generation: int
    new_generation: int
    late_event_ids: tuple[str, ...]
    new_state_version: int
    new_lease_id: str
    new_context_hash: str
    decision_count_before: int
    decision_count_after: int
    callback_count_before: int
    callback_count_after: int


@dataclass(frozen=True)
class RecoveryEvidence:
    session_id: str
    gateway_id: str
    gap: GapProbe
    old_generation: OldGenerationProbe


def _walk_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return set(value) | {key for item in value.values() for key in _walk_keys(item)}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return {key for item in value for key in _walk_keys(item)}
    return set()


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = tuple(_required_string(item, field) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} must contain unique values")
    return result


def _metrics(value: object, field: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    parsed: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"{field} must contain integer metrics")
        parsed[key] = item
    return parsed


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _sha256_hex(value: object, field: str) -> str:
    parsed = _required_string(value, field).lower()
    if len(parsed) != 64 or any(character not in "0123456789abcdef" for character in parsed):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return parsed


def parse_session_evidence(value: object) -> SessionEvidence:
    if not isinstance(value, Mapping):
        raise ValueError("session evidence must be an object")
    forbidden = _walk_keys(value) & _FORBIDDEN_EVIDENCE_KEYS
    if forbidden:
        raise ValueError("session evidence contains forbidden fields")
    generation = value.get("controlGeneration")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ValueError("controlGeneration must be a positive integer")
    raw_events = value.get("eventIdsByType")
    if not isinstance(raw_events, Mapping):
        raise ValueError("eventIdsByType must be an object")
    events = {
        _required_string(event_type, "eventType"): _string_tuple(ids, f"eventIdsByType.{event_type}")
        for event_type, ids in raw_events.items()
    }
    decisions = _string_tuple(value.get("decisionIds"), "decisionIds")
    if len(decisions) != 2:
        raise ValueError("decisionIds must contain exactly two values")
    skill_call_ids = _string_tuple(value.get("skillCallIds"), "skillCallIds")
    return SessionEvidence(
        session_id=_required_string(value.get("sessionId"), "sessionId"),
        gateway_id=_required_string(value.get("gatewayId"), "gatewayId"),
        control_generation=generation,
        event_ids_by_type=events,
        decision_ids=(decisions[0], decisions[1]),
        skill_call_ids=skill_call_ids,
        metrics_before=_metrics(value.get("metricsBefore"), "metricsBefore"),
        metrics_after=_metrics(value.get("metricsAfter"), "metricsAfter"),
    )


def parse_recovery_evidence(value: object) -> RecoveryEvidence:
    if not isinstance(value, Mapping):
        raise ValueError("recovery evidence must be an object")
    forbidden = _walk_keys(value) & _FORBIDDEN_EVIDENCE_KEYS
    if forbidden:
        raise ValueError("recovery evidence contains forbidden fields")

    raw_gap = value.get("gapProbe")
    if not isinstance(raw_gap, Mapping):
        raise ValueError("gapProbe must be an object")
    gap_event_ids = _string_tuple(raw_gap.get("eventIds"), "gapProbe.eventIds")
    if len(gap_event_ids) < 2:
        raise ValueError("gapProbe.eventIds must contain at least two values")

    raw_old = value.get("oldGenerationProbe")
    if not isinstance(raw_old, Mapping):
        raise ValueError("oldGenerationProbe must be an object")
    old_generation = _positive_int(raw_old.get("oldGeneration"), "oldGenerationProbe.oldGeneration")
    new_generation = _positive_int(raw_old.get("newGeneration"), "oldGenerationProbe.newGeneration")
    if new_generation <= old_generation:
        raise ValueError("newGeneration must be greater than oldGeneration")
    late_event_ids = _string_tuple(
        raw_old.get("lateEventIds"),
        "oldGenerationProbe.lateEventIds",
    )
    if not late_event_ids:
        raise ValueError("oldGenerationProbe.lateEventIds must not be empty")

    return RecoveryEvidence(
        session_id=_required_string(value.get("sessionId"), "sessionId"),
        gateway_id=_required_string(value.get("gatewayId"), "gatewayId"),
        gap=GapProbe(
            control_generation=_positive_int(
                raw_gap.get("controlGeneration"),
                "gapProbe.controlGeneration",
            ),
            event_ids=gap_event_ids,
        ),
        old_generation=OldGenerationProbe(
            old_generation=old_generation,
            new_generation=new_generation,
            late_event_ids=late_event_ids,
            new_state_version=_nonnegative_int(
                raw_old.get("newStateVersion"),
                "oldGenerationProbe.newStateVersion",
            ),
            new_lease_id=_required_string(
                raw_old.get("newLeaseId"),
                "oldGenerationProbe.newLeaseId",
            ),
            new_context_hash=_sha256_hex(
                raw_old.get("newContextHash"),
                "oldGenerationProbe.newContextHash",
            ),
            decision_count_before=_nonnegative_int(
                raw_old.get("decisionCountBeforeLateEvents"),
                "oldGenerationProbe.decisionCountBeforeLateEvents",
            ),
            decision_count_after=_nonnegative_int(
                raw_old.get("decisionCountAfterLateEvents"),
                "oldGenerationProbe.decisionCountAfterLateEvents",
            ),
            callback_count_before=_nonnegative_int(
                raw_old.get("callbackCountBeforeLateEvents"),
                "oldGenerationProbe.callbackCountBeforeLateEvents",
            ),
            callback_count_after=_nonnegative_int(
                raw_old.get("callbackCountAfterLateEvents"),
                "oldGenerationProbe.callbackCountAfterLateEvents",
            ),
        ),
    )


async def _scalar(engine: AsyncEngine, statement: str, parameters: Mapping[str, Any]) -> Any:
    async with engine.connect() as connection:
        return await connection.scalar(sa.text(statement), parameters)


async def _rows(engine: AsyncEngine, statement: str, parameters: Mapping[str, Any]) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        result = await connection.execute(sa.text(statement), parameters)
        return [dict(row) for row in result.mappings().all()]


async def assert_empty_runtime(engine: AsyncEngine, revision: str | None) -> dict[str, Any]:
    current_revision = str(await _scalar(engine, "SELECT version_num FROM alembic_version", {}))
    if revision is not None and current_revision != revision:
        raise AssertionError("database revision does not match expectation")
    counts = {table: int(await _scalar(engine, f"SELECT count(*) FROM {table}", {})) for table in _RUNTIME_TABLES}
    if any(counts.values()):
        raise AssertionError("v2 runtime tables are not empty")
    return {"revision": current_revision, "counts": counts}


def _metric_delta(evidence: SessionEvidence, name: str) -> int:
    return evidence.metrics_after.get(name, 0) - evidence.metrics_before.get(name, 0)


async def assert_complete_cycle(engine: AsyncEngine, evidence: SessionEvidence) -> dict[str, Any]:
    parameters = {
        "gateway_id": evidence.gateway_id,
        "session_id": evidence.session_id,
        "generation": evidence.control_generation,
    }
    sessions = await _rows(
        engine,
        "SELECT * FROM llm_gateway_sessions WHERE gateway_id=:gateway_id AND session_id=:session_id",
        parameters,
    )
    if len(sessions) != 1 or sessions[0]["status"] != "stopped":
        raise AssertionError("target session must exist exactly once and be stopped")
    if int(sessions[0]["current_generation"]) != evidence.control_generation:
        raise AssertionError("target session generation mismatch")

    cycles = await _rows(
        engine,
        """
        SELECT * FROM llm_gateway_control_cycles
        WHERE gateway_id=:gateway_id AND session_id=:session_id AND control_generation=:generation
        """,
        parameters,
    )
    if len(cycles) != 1 or cycles[0]["status"] != "stopped":
        raise AssertionError("target cycle must exist exactly once and be stopped")

    expected_event_ids = {event_id for event_ids in evidence.event_ids_by_type.values() for event_id in event_ids}
    events = await _rows(
        engine,
        """
        SELECT event_id, event_type, event_sequence, status
        FROM llm_gateway_events
        WHERE gateway_id=:gateway_id AND session_id=:session_id AND control_generation=:generation
        """,
        parameters,
    )
    if {str(row["event_id"]) for row in events} != expected_event_ids:
        raise AssertionError("database event IDs do not match session evidence")
    if any(row["status"] != "succeeded" for row in events):
        raise AssertionError("all target events must be succeeded")
    event_types = [str(row["event_type"]) for row in events]
    for required in ("session_started", "skill_started", "skill_finished", "session_stopped"):
        if event_types.count(required) != 1:
            raise AssertionError(f"{required} must occur exactly once")
    if event_types.count("decision_rejected") != 0:
        raise AssertionError("decision_rejected must not occur")
    max_sequence = max(int(row["event_sequence"]) for row in events)
    if int(cycles[0]["next_event_sequence"]) != max_sequence + 1:
        raise AssertionError("cycle next_event_sequence mismatch")

    decisions = await _rows(
        engine,
        """
        SELECT decision_id, decision_lease_id, source_event_id, status, request_body_bytes, body_hash
        FROM llm_gateway_decisions
        WHERE gateway_id=:gateway_id AND session_id=:session_id AND control_generation=:generation
        ORDER BY created_at
        """,
        parameters,
    )
    if len(decisions) != 2 or tuple(str(row["decision_id"]) for row in decisions) != evidence.decision_ids:
        raise AssertionError("database decisions do not match session evidence")
    if any(row["status"] != "accepted" for row in decisions):
        raise AssertionError("both decisions must be accepted")
    if len({row["source_event_id"] for row in decisions}) != 2:
        raise AssertionError("decision source events must be unique")
    if len({row["decision_lease_id"] for row in decisions}) != 2:
        raise AssertionError("decision leases must be unique")
    decision_hashes: dict[str, str] = {}
    for row in decisions:
        actual_hash = hashlib.sha256(bytes(row["request_body_bytes"])).hexdigest()
        if actual_hash != row["body_hash"]:
            raise AssertionError("decision body hash mismatch")
        decision_hashes[str(row["decision_id"])] = actual_hash

    skill_calls = await _rows(
        engine,
        """
        SELECT skill_call_id, status, terminal_event_id
        FROM llm_gateway_skill_calls
        WHERE gateway_id=:gateway_id AND session_id=:session_id
        ORDER BY created_at
        """,
        parameters,
    )
    expected_call_ids = set(evidence.skill_call_ids)
    actual_call_ids = {str(row["skill_call_id"]) for row in skill_calls}
    if not expected_call_ids.issubset(actual_call_ids):
        raise AssertionError("observed skill calls are missing from database")
    primary = [row for row in skill_calls if str(row["skill_call_id"]) in expected_call_ids]
    if len(primary) != len(expected_call_ids):
        raise AssertionError("observed skill calls must be unique")
    if any(row["status"] != "succeeded" or row["terminal_event_id"] is None for row in primary):
        raise AssertionError("observed skill calls must have succeeded terminal events")
    if any(row["status"] in {"pending", "started"} for row in skill_calls):
        raise AssertionError("cycle must not retain pending or started skill calls")
    if any(row["status"] not in {"succeeded", "cancelled"} for row in skill_calls):
        raise AssertionError("unexpected terminal skill call status")

    if _metric_delta(evidence, "llmEventsFailed") != 0:
        raise AssertionError("llmEventsFailed must not increase")
    if _metric_delta(evidence, "llmDecisionsRejected") != 0:
        raise AssertionError("llmDecisionsRejected must not increase")
    if _metric_delta(evidence, "llmDecisionsAccepted") != 2:
        raise AssertionError("llmDecisionsAccepted must increase by exactly two")

    return {
        "sessionId": evidence.session_id,
        "gatewayId": evidence.gateway_id,
        "controlGeneration": evidence.control_generation,
        "eventCount": len(events),
        "eventStatuses": sorted({str(row["status"]) for row in events}),
        "decisionCount": len(decisions),
        "decisionStatuses": sorted({str(row["status"]) for row in decisions}),
        "decisionBodyHashes": decision_hashes,
        "skillCallCount": len(skill_calls),
        "skillCallStatuses": sorted({str(row["status"]) for row in skill_calls}),
    }


async def assert_gap_recovered(engine: AsyncEngine, evidence: RecoveryEvidence) -> dict[str, Any]:
    parameters = {
        "gateway_id": evidence.gateway_id,
        "session_id": evidence.session_id,
        "generation": evidence.gap.control_generation,
        "event_ids": list(evidence.gap.event_ids),
    }
    events = await _rows(
        engine,
        """
        SELECT event_id, event_sequence, status
        FROM llm_gateway_events
        WHERE gateway_id=:gateway_id AND session_id=:session_id
          AND control_generation=:generation AND event_id = ANY(:event_ids)
        ORDER BY event_sequence
        """,
        parameters,
    )
    if tuple(str(row["event_id"]) for row in events) != evidence.gap.event_ids:
        raise AssertionError("gap event IDs must be persisted and processed in sequence order")
    sequences = [int(row["event_sequence"]) for row in events]
    if sequences != list(range(1, len(events) + 1)):
        raise AssertionError("gap event sequence must be contiguous from one")
    if any(row["status"] != "succeeded" for row in events):
        raise AssertionError("all gap events must succeed after the missing event arrives")
    cycles = await _rows(
        engine,
        """
        SELECT status, next_event_sequence
        FROM llm_gateway_control_cycles
        WHERE gateway_id=:gateway_id AND session_id=:session_id
          AND control_generation=:generation
        """,
        parameters,
    )
    if len(cycles) != 1 or int(cycles[0]["next_event_sequence"]) <= max(sequences):
        raise AssertionError("gap cycle did not advance beyond the recovered sequence")
    return {
        "gapEventIds": list(evidence.gap.event_ids),
        "gapEventStatuses": sorted({str(row["status"]) for row in events}),
        "gapNextEventSequence": int(cycles[0]["next_event_sequence"]),
    }


def _context_hash(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


async def assert_old_generation_superseded(
    engine: AsyncEngine,
    evidence: RecoveryEvidence,
) -> dict[str, Any]:
    probe = evidence.old_generation
    parameters = {
        "gateway_id": evidence.gateway_id,
        "session_id": evidence.session_id,
        "old_generation": probe.old_generation,
        "new_generation": probe.new_generation,
        "late_event_ids": list(probe.late_event_ids),
    }
    sessions = await _rows(
        engine,
        """
        SELECT current_generation, status
        FROM llm_gateway_sessions
        WHERE gateway_id=:gateway_id AND session_id=:session_id
        """,
        parameters,
    )
    if len(sessions) != 1 or int(sessions[0]["current_generation"]) != probe.new_generation:
        raise AssertionError("new generation must remain current")
    cycles = await _rows(
        engine,
        """
        SELECT control_generation, status, latest_state_version,
               latest_decision_lease_id, latest_decision_context
        FROM llm_gateway_control_cycles
        WHERE gateway_id=:gateway_id AND session_id=:session_id
          AND control_generation IN (:old_generation, :new_generation)
        ORDER BY control_generation
        """,
        parameters,
    )
    cycles_by_generation = {int(row["control_generation"]): row for row in cycles}
    old_cycle = cycles_by_generation.get(probe.old_generation)
    new_cycle = cycles_by_generation.get(probe.new_generation)
    if old_cycle is None or old_cycle["status"] != "superseded" or new_cycle is None:
        raise AssertionError("generation cycle statuses do not prove supersession")
    if (
        int(new_cycle["latest_state_version"]) != probe.new_state_version
        or new_cycle["latest_decision_lease_id"] != probe.new_lease_id
        or _context_hash(new_cycle["latest_decision_context"]) != probe.new_context_hash
    ):
        raise AssertionError("new generation lease context changed after stale events")

    late_events = await _rows(
        engine,
        """
        SELECT id, event_id, status
        FROM llm_gateway_events
        WHERE gateway_id=:gateway_id AND session_id=:session_id
          AND control_generation=:old_generation AND event_id = ANY(:late_event_ids)
        ORDER BY event_id
        """,
        parameters,
    )
    if {str(row["event_id"]) for row in late_events} != set(probe.late_event_ids):
        raise AssertionError("late old-generation events are missing")
    if any(row["status"] != "superseded" for row in late_events):
        raise AssertionError("late old-generation events must be superseded")
    stale_decisions = int(
        await _scalar(
            engine,
            """
            SELECT count(*) FROM llm_gateway_decisions d
            JOIN llm_gateway_events e ON e.id=d.source_event_id
            WHERE e.gateway_id=:gateway_id AND e.event_id = ANY(:late_event_ids)
            """,
            parameters,
        )
    )
    decision_count = int(
        await _scalar(
            engine,
            """
            SELECT count(*) FROM llm_gateway_decisions
            WHERE gateway_id=:gateway_id AND session_id=:session_id
            """,
            parameters,
        )
    )
    if stale_decisions != 0:
        raise AssertionError("stale events must not generate decisions")
    if probe.decision_count_before != probe.decision_count_after or decision_count != probe.decision_count_after:
        raise AssertionError("decision count changed after stale events")
    if probe.callback_count_before != probe.callback_count_after:
        raise AssertionError("Gateway callback count changed after stale events")
    return {
        "currentGeneration": probe.new_generation,
        "oldCycleStatus": str(old_cycle["status"]),
        "lateEventIds": list(probe.late_event_ids),
        "lateEventStatuses": sorted({str(row["status"]) for row in late_events}),
        "newContextHash": probe.new_context_hash,
        "decisionCount": decision_count,
        "staleDecisionCount": stale_decisions,
        "callbackCount": probe.callback_count_after,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight-test-database", action="store_true")
    parser.add_argument("--expect-revision")
    parser.add_argument("--expect-empty", action="store_true")
    parser.add_argument("--session-file", type=Path)
    parser.add_argument("--expect-complete-cycle", action="store_true")
    parser.add_argument("--expect-gap-recovered", action="store_true")
    parser.add_argument("--expect-old-generation-superseded", action="store_true")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    require_test_database_url()
    engine = await open_verified_test_engine()
    try:
        if args.preflight_test_database and not (args.expect_empty or args.expect_complete_cycle or args.session_file):
            return {"safeTestDatabase": True}
        if args.expect_empty:
            return await assert_empty_runtime(engine, args.expect_revision)
        if args.expect_complete_cycle:
            if args.session_file is None:
                raise ValueError("--session-file is required with --expect-complete-cycle")
            evidence = parse_session_evidence(json.loads(args.session_file.read_text(encoding="utf-8-sig")))
            return await assert_complete_cycle(engine, evidence)
        if args.expect_gap_recovered or args.expect_old_generation_superseded:
            if args.session_file is None:
                raise ValueError("--session-file is required for recovery assertions")
            evidence = parse_recovery_evidence(
                json.loads(args.session_file.read_text(encoding="utf-8-sig"))
            )
            result: dict[str, Any] = {}
            if args.expect_gap_recovered:
                result.update(await assert_gap_recovered(engine, evidence))
            if args.expect_old_generation_superseded:
                result.update(await assert_old_generation_superseded(engine, evidence))
            return result
        raise ValueError("an assertion mode is required")
    finally:
        await engine.dispose()


def main() -> int:
    args = _parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as error:
        print(json.dumps({"success": False, "category": type(error).__name__}, separators=(",", ":")))
        return 1
    print(json.dumps({"success": True, **result}, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
