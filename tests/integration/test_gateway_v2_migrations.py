"""Schema contract tests for the LLM Gateway v2 durable inbox."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

INBOX_TABLES = {
    "llm_gateway_sessions",
    "llm_gateway_control_cycles",
    "llm_gateway_events",
}
OUTBOX_TABLES = {
    "llm_gateway_decisions",
    "llm_gateway_skill_calls",
}
V2_TABLES = INBOX_TABLES | OUTBOX_TABLES


async def _with_connection(database_url: URL, operation: Callable[[sa.ext.asyncio.AsyncConnection], Any]) -> Any:
    engine = create_async_engine(database_url, poolclass=sa.pool.NullPool)
    try:
        async with engine.connect() as connection:
            return await operation(connection)
    finally:
        await engine.dispose()


def _run_with_connection(database_url: URL, operation: Callable[[sa.ext.asyncio.AsyncConnection], Any]) -> Any:
    return asyncio.run(_with_connection(database_url, operation))


def _inspect_schema(database_url: URL) -> dict[str, Any]:
    async def inspect_connection(connection: sa.ext.asyncio.AsyncConnection) -> dict[str, Any]:
        def collect(sync_connection: Connection) -> dict[str, Any]:
            inspector = sa.inspect(sync_connection)
            tables = set(inspector.get_table_names())
            table_details: dict[str, Any] = {}
            for table_name in V2_TABLES & tables:
                table_details[table_name] = {
                    "columns": {column["name"]: column for column in inspector.get_columns(table_name)},
                    "foreign_keys": inspector.get_foreign_keys(table_name),
                    "checks": inspector.get_check_constraints(table_name),
                    "uniques": inspector.get_unique_constraints(table_name),
                    "indexes": inspector.get_indexes(table_name),
                    "primary_key": inspector.get_pk_constraint(table_name),
                }
            return {"tables": tables, "details": table_details}

        return await connection.run_sync(collect)

    return _run_with_connection(database_url, inspect_connection)


def _query_mappings(database_url: URL, statement: sa.TextClause, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    async def query(connection: sa.ext.asyncio.AsyncConnection) -> list[dict[str, Any]]:
        result = await connection.execute(statement, parameters)
        return [dict(row) for row in result.mappings().all()]

    return _run_with_connection(database_url, query)


def _current_database(database_url: URL) -> str:
    rows = _query_mappings(database_url, sa.text("SELECT current_database() AS database_name"), {})
    return str(rows[0]["database_name"])


def _current_revision(database_url: URL) -> str:
    rows = _query_mappings(database_url, sa.text("SELECT version_num FROM alembic_version"), {})
    return str(rows[0]["version_num"])


def _catalog_constraints(database_url: URL, table_name: str) -> dict[str, str]:
    rows = _query_mappings(
        database_url,
        sa.text(
            """
            SELECT constraint_row.conname AS name,
                   pg_get_constraintdef(constraint_row.oid) AS definition
            FROM pg_constraint AS constraint_row
            WHERE constraint_row.conrelid = to_regclass(:table_name)
            """
        ),
        {"table_name": table_name},
    )
    return {str(row["name"]): str(row["definition"]) for row in rows}


def _catalog_indexes(database_url: URL, table_name: str) -> dict[str, str]:
    rows = _query_mappings(
        database_url,
        sa.text(
            """
            SELECT indexname AS name, indexdef AS definition
            FROM pg_indexes
            WHERE schemaname = current_schema() AND tablename = :table_name
            """
        ),
        {"table_name": table_name},
    )
    return {str(row["name"]): str(row["definition"]) for row in rows}


def _assert_default(column: dict[str, Any], expected_fragment: str | None) -> None:
    actual = column["default"]
    if expected_fragment is None:
        assert actual is None
        return
    assert actual is not None
    assert expected_fragment in str(actual).lower()


def _assert_column(
    columns: dict[str, dict[str, Any]],
    name: str,
    expected_type: type[sa.types.TypeEngine[Any]],
    *,
    nullable: bool,
    default: str | None = None,
    length: int | None = None,
    timezone: bool | None = None,
) -> None:
    column = columns[name]
    assert isinstance(column["type"], expected_type)
    assert column["nullable"] is nullable
    if length is not None:
        assert column["type"].length == length
    if timezone is not None:
        assert column["type"].timezone is timezone
    _assert_default(column, default)


def _assert_primary_key(details: dict[str, Any], expected_name: str) -> None:
    assert details["primary_key"]["name"] == expected_name
    assert details["primary_key"]["constrained_columns"] == ["id"]


def _assert_foreign_keys(details: dict[str, Any], expected: dict[str, tuple[list[str], str, str]]) -> None:
    actual = {
        foreign_key["name"]: (
            foreign_key["constrained_columns"],
            foreign_key["referred_table"],
            foreign_key["referred_columns"],
            foreign_key["options"].get("ondelete"),
        )
        for foreign_key in details["foreign_keys"]
    }
    assert actual == {
        name: (columns, referred_table, ["id"], ondelete)
        for name, (columns, referred_table, ondelete) in expected.items()
    }


def _assert_named_unique(details: dict[str, Any], name: str, columns: list[str]) -> None:
    uniques = {constraint["name"]: constraint["column_names"] for constraint in details["uniques"]}
    assert uniques[name] == columns


def _assert_index(details: dict[str, Any], name: str, columns: list[str], *, unique: bool = False) -> None:
    indexes = {index["name"]: index for index in details["indexes"]}
    assert indexes[name]["column_names"] == columns
    assert indexes[name]["unique"] is unique


def _assert_check_tokens(database_url: URL, table_name: str, expected: dict[str, tuple[str, ...]]) -> None:
    constraints = _catalog_constraints(database_url, table_name)
    for name, tokens in expected.items():
        definition = constraints[name].lower()
        for token in tokens:
            assert token.lower() in definition


def _assert_sessions(database_url: URL, details: dict[str, Any]) -> None:
    columns = details["columns"]
    expected_columns = {
        "id",
        "tenant_id",
        "gateway_id",
        "session_id",
        "current_generation",
        "fence_version",
        "status",
        "created_at",
        "updated_at",
    }
    if "last_event_at" in columns:
        expected_columns.add("last_event_at")
    assert set(columns) == expected_columns
    _assert_column(columns, "id", sa.UUID, nullable=False, default="gen_random_uuid()")
    _assert_column(columns, "tenant_id", sa.UUID, nullable=False)
    _assert_column(columns, "gateway_id", sa.String, nullable=False, length=128)
    _assert_column(columns, "session_id", sa.String, nullable=False, length=128)
    _assert_column(columns, "current_generation", sa.BigInteger, nullable=True)
    _assert_column(columns, "fence_version", sa.BigInteger, nullable=False, default="0")
    _assert_column(columns, "status", sa.String, nullable=False, default="'pending'", length=24)
    _assert_column(columns, "created_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_column(columns, "updated_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    if "last_event_at" in columns:
        _assert_column(columns, "last_event_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_primary_key(details, "pk_llm_gateway_sessions")
    _assert_foreign_keys(details, {"fk_llm_gateway_sessions_tenant": (["tenant_id"], "tenants", "CASCADE")})
    _assert_named_unique(details, "uq_llm_gateway_sessions_identity", ["gateway_id", "session_id"])
    _assert_index(details, "ix_llm_gateway_sessions_tenant_status", ["tenant_id", "status"])
    if "last_event_at" in columns:
        _assert_index(details, "ix_llm_gateway_sessions_liveness", ["status", "last_event_at"])
    _assert_check_tokens(
        database_url,
        "llm_gateway_sessions",
        {
            "ck_llm_gateway_sessions_current_generation_positive": ("current_generation", "> 0"),
            "ck_llm_gateway_sessions_fence_version_nonnegative": ("fence_version", ">= 0"),
            "ck_llm_gateway_sessions_status": ("pending", "active", "stopped", "manual"),
        },
    )


def _assert_cycles(
    database_url: URL,
    details: dict[str, Any],
    *,
    activity_plan: bool = False,
) -> None:
    columns = details["columns"]
    expected_columns = {
        "id",
        "tenant_id",
        "runtime_session_id",
        "gateway_id",
        "session_id",
        "control_generation",
        "status",
        "next_event_sequence",
        "latest_state_version",
        "latest_decision_lease_id",
        "latest_decision_context",
        "created_at",
        "updated_at",
        "started_at",
        "stopped_at",
    }
    if activity_plan:
        expected_columns.update(
            {
                "activity_plan_id",
                "activity_goal",
                "activity_plan",
                "activity_phase",
                "activity_status",
                "activity_current_step_id",
                "activity_plan_version",
                "activity_last_event_id",
                "activity_last_event_sequence",
            }
        )
    assert set(columns) == expected_columns
    _assert_column(columns, "id", sa.UUID, nullable=False)
    _assert_column(columns, "tenant_id", sa.UUID, nullable=False)
    _assert_column(columns, "runtime_session_id", sa.UUID, nullable=False)
    _assert_column(columns, "gateway_id", sa.String, nullable=False, length=128)
    _assert_column(columns, "session_id", sa.String, nullable=False, length=128)
    _assert_column(columns, "control_generation", sa.BigInteger, nullable=False)
    _assert_column(columns, "status", sa.String, nullable=False, length=24)
    _assert_column(columns, "next_event_sequence", sa.BigInteger, nullable=False, default="1")
    _assert_column(columns, "latest_state_version", sa.BigInteger, nullable=True)
    _assert_column(columns, "latest_decision_lease_id", sa.String, nullable=True, length=128)
    _assert_column(columns, "latest_decision_context", JSONB, nullable=True)
    _assert_column(columns, "created_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_column(columns, "updated_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_column(columns, "started_at", sa.DateTime, nullable=True, timezone=True)
    _assert_column(columns, "stopped_at", sa.DateTime, nullable=True, timezone=True)
    if activity_plan:
        _assert_column(columns, "activity_plan_id", sa.String, nullable=True, length=128)
        _assert_column(columns, "activity_goal", JSONB, nullable=True)
        _assert_column(columns, "activity_plan", JSONB, nullable=True)
        _assert_column(columns, "activity_phase", sa.String, nullable=True, length=64)
        _assert_column(columns, "activity_status", sa.String, nullable=False, default="'inactive'", length=24)
        _assert_column(columns, "activity_current_step_id", sa.String, nullable=True, length=128)
        _assert_column(columns, "activity_plan_version", sa.BigInteger, nullable=False, default="0")
        _assert_column(columns, "activity_last_event_id", sa.UUID, nullable=True)
        _assert_column(columns, "activity_last_event_sequence", sa.BigInteger, nullable=True)
    _assert_primary_key(details, "pk_llm_gateway_control_cycles")
    _assert_foreign_keys(
        details,
        {
            "fk_llm_gateway_cycles_tenant": (["tenant_id"], "tenants", "CASCADE"),
            "fk_llm_gateway_cycles_runtime_session": (
                ["runtime_session_id"],
                "llm_gateway_sessions",
                "CASCADE",
            ),
        },
    )
    _assert_named_unique(
        details,
        "uq_llm_gateway_cycles_partition",
        ["gateway_id", "session_id", "control_generation"],
    )
    _assert_index(
        details,
        "ix_llm_gateway_cycles_runnable",
        ["status", "next_event_sequence", "updated_at"],
    )
    _assert_check_tokens(
        database_url,
        "llm_gateway_control_cycles",
        {
            "ck_llm_gateway_cycles_control_generation_positive": ("control_generation", "> 0"),
            "ck_llm_gateway_cycles_next_event_sequence_positive": ("next_event_sequence", "> 0"),
            "ck_llm_gateway_cycles_latest_state_version_nonnegative": ("latest_state_version", ">= 0"),
            "ck_llm_gateway_cycles_status": ("pending", "active", "stopped", "superseded", "manual"),
            **(
                {
                    "ck_llm_gateway_cycles_activity_status": (
                        "inactive",
                        "active",
                        "completed",
                        "paused",
                        "abandoned",
                    ),
                    "ck_llm_gateway_cycles_activity_plan_version_nonnegative": (
                        "activity_plan_version",
                        ">= 0",
                    ),
                    "ck_llm_gateway_cycles_activity_last_sequence_positive": (
                        "activity_last_event_sequence",
                        "> 0",
                    ),
                }
                if activity_plan
                else {}
            ),
        },
    )


def _assert_events(
    database_url: URL,
    details: dict[str, Any],
    *,
    processing_index: bool = True,
    hosted_chat: bool = False,
) -> None:
    columns = details["columns"]
    assert set(columns) == {
        "id",
        "tenant_id",
        "cycle_id",
        "gateway_id",
        "session_id",
        "event_id",
        "event_type",
        "control_generation",
        "event_sequence",
        "content_hash",
        "event_body",
        "trace_id",
        "status",
        "attempt_count",
        "next_attempt_at",
        "claim_token",
        "claimed_fence_version",
        "lock_until",
        "locked_by",
        "error_stage",
        "error_category",
        "received_at",
        "updated_at",
        "started_at",
        "completed_at",
    }
    _assert_column(columns, "id", sa.UUID, nullable=False)
    _assert_column(columns, "tenant_id", sa.UUID, nullable=False)
    _assert_column(columns, "cycle_id", sa.UUID, nullable=False)
    _assert_column(columns, "gateway_id", sa.String, nullable=False, length=128)
    _assert_column(columns, "session_id", sa.String, nullable=False, length=128)
    _assert_column(columns, "event_id", sa.String, nullable=False, length=128)
    _assert_column(columns, "event_type", sa.String, nullable=False, length=32)
    _assert_column(columns, "control_generation", sa.BigInteger, nullable=False)
    _assert_column(columns, "event_sequence", sa.BigInteger, nullable=False)
    _assert_column(columns, "content_hash", sa.CHAR, nullable=False, length=64)
    _assert_column(columns, "event_body", JSONB, nullable=False)
    _assert_column(columns, "trace_id", sa.String, nullable=False, length=128)
    _assert_column(columns, "status", sa.String, nullable=False, length=32)
    _assert_column(columns, "attempt_count", sa.Integer, nullable=False, default="0")
    _assert_column(columns, "next_attempt_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_column(columns, "claim_token", sa.UUID, nullable=True)
    _assert_column(columns, "claimed_fence_version", sa.BigInteger, nullable=True)
    _assert_column(columns, "lock_until", sa.DateTime, nullable=True, timezone=True)
    _assert_column(columns, "locked_by", sa.String, nullable=True, length=128)
    _assert_column(columns, "error_stage", sa.String, nullable=True, length=64)
    _assert_column(columns, "error_category", sa.String, nullable=True, length=64)
    _assert_column(columns, "received_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_column(columns, "updated_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_column(columns, "started_at", sa.DateTime, nullable=True, timezone=True)
    _assert_column(columns, "completed_at", sa.DateTime, nullable=True, timezone=True)
    _assert_primary_key(details, "pk_llm_gateway_events")
    _assert_foreign_keys(
        details,
        {
            "fk_llm_gateway_events_tenant": (["tenant_id"], "tenants", "CASCADE"),
            "fk_llm_gateway_events_cycle": (["cycle_id"], "llm_gateway_control_cycles", "CASCADE"),
        },
    )
    _assert_named_unique(details, "uq_llm_gateway_events_identity", ["gateway_id", "event_id"])
    _assert_named_unique(
        details,
        "uq_llm_gateway_events_partition_sequence",
        ["gateway_id", "session_id", "control_generation", "event_sequence"],
    )
    _assert_index(details, "ix_llm_gateway_events_due", ["status", "next_attempt_at", "received_at"])
    indexes = _catalog_indexes(database_url, "llm_gateway_events")
    if processing_index:
        _assert_index(details, "uq_llm_gateway_events_cycle_processing", ["cycle_id"], unique=True)
        partial_index = indexes["uq_llm_gateway_events_cycle_processing"].lower()
        assert "create unique index" in partial_index
        assert "where" in partial_index
        assert "status" in partial_index
        assert "processing" in partial_index
    else:
        assert "uq_llm_gateway_events_cycle_processing" not in indexes
    _assert_check_tokens(
        database_url,
        "llm_gateway_events",
        {
            "ck_llm_gateway_events_event_type": (
                "session_started",
                "observation_updated",
                "skill_started",
                "skill_finished",
                "decision_rejected",
                "session_stopped",
            )
            + (
                ("chat_received", "nearby_friend_chat_requested", "chat_send_result")
                if hosted_chat
                else ()
            ),
            "ck_llm_gateway_events_event_sequence_positive": ("event_sequence", "> 0"),
            "ck_llm_gateway_events_status": (
                "pending",
                "processing",
                "succeeded",
                "retryable_failed",
                "dead_letter",
                "manual",
                "superseded",
            ),
            "ck_llm_gateway_events_session_started_sequence": (
                "event_type",
                "session_started",
                "event_sequence",
                "= 1",
                "> 1",
            ),
        },
    )
def _assert_decisions(
    database_url: URL,
    details: dict[str, Any],
    *,
    activity_plan: bool = False,
    activity_capacity: bool = False,
    monitoring: bool = False,
) -> None:
    columns = details["columns"]
    expected_columns = {
        "id",
        "tenant_id",
        "cycle_id",
        "source_event_id",
        "action_tracking_id",
        "gateway_id",
        "session_id",
        "decision_id",
        "decision_lease_id",
        "control_generation",
        "state_version",
        "lease_expires_at_ms",
        "action",
        "request_body_json",
        "request_body_bytes",
        "body_hash",
        "status",
        "attempt_count",
        "next_attempt_at",
        "claim_token",
        "claimed_fence_version",
        "locked_by",
        "lock_until",
        "response_http_status",
        "response_status",
        "response_reason",
        "skill_call_id",
        "error_stage",
        "error_category",
        "created_at",
        "updated_at",
        "sent_at",
        "completed_at",
    }
    if activity_plan:
        expected_columns.update(
            {
                "activity_plan_id",
                "activity_plan_version",
                "activity_step_id",
                "activity_phase",
            }
        )
    if activity_capacity:
        expected_columns.update(
            {
                "activity_capacity_key",
                "activity_capacity_limit",
                "activity_capacity_expires_at",
            }
        )
    if monitoring:
        expected_columns.update(
            {
                "response_body_json",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "model_calls",
                "usage_reported_calls",
                "usage_missing_calls",
            }
        )
    assert set(columns) == expected_columns
    _assert_column(columns, "id", sa.UUID, nullable=False, default="gen_random_uuid()")
    _assert_column(columns, "tenant_id", sa.UUID, nullable=False)
    _assert_column(columns, "cycle_id", sa.UUID, nullable=False)
    _assert_column(columns, "source_event_id", sa.UUID, nullable=False)
    _assert_column(columns, "action_tracking_id", sa.UUID, nullable=True)
    for name in ("gateway_id", "session_id", "decision_id", "decision_lease_id"):
        _assert_column(columns, name, sa.String, nullable=False, length=128)
    _assert_column(columns, "control_generation", sa.BigInteger, nullable=False)
    _assert_column(columns, "state_version", sa.BigInteger, nullable=False)
    _assert_column(columns, "lease_expires_at_ms", sa.BigInteger, nullable=True)
    _assert_column(columns, "action", sa.String, nullable=False, length=24)
    _assert_column(columns, "request_body_json", JSONB, nullable=False)
    _assert_column(columns, "request_body_bytes", BYTEA, nullable=False)
    _assert_column(columns, "body_hash", sa.CHAR, nullable=False, length=64)
    _assert_column(columns, "status", sa.String, nullable=False, length=32)
    _assert_column(columns, "attempt_count", sa.Integer, nullable=False, default="0")
    _assert_column(columns, "next_attempt_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_column(columns, "claim_token", sa.UUID, nullable=True)
    _assert_column(columns, "claimed_fence_version", sa.BigInteger, nullable=True)
    _assert_column(columns, "locked_by", sa.String, nullable=True, length=128)
    _assert_column(columns, "lock_until", sa.DateTime, nullable=True, timezone=True)
    _assert_column(columns, "response_http_status", sa.Integer, nullable=True)
    _assert_column(columns, "response_status", sa.String, nullable=True, length=32)
    _assert_column(columns, "response_reason", sa.String, nullable=True, length=256)
    _assert_column(columns, "skill_call_id", sa.String, nullable=True, length=128)
    if activity_plan:
        _assert_column(columns, "activity_plan_id", sa.String, nullable=True, length=128)
        _assert_column(columns, "activity_plan_version", sa.BigInteger, nullable=True)
        _assert_column(columns, "activity_step_id", sa.String, nullable=True, length=128)
        _assert_column(columns, "activity_phase", sa.String, nullable=True, length=64)
    if activity_capacity:
        _assert_column(columns, "activity_capacity_key", sa.String, nullable=True, length=512)
        _assert_column(columns, "activity_capacity_limit", sa.Integer, nullable=True)
        _assert_column(
            columns,
            "activity_capacity_expires_at",
            sa.DateTime,
            nullable=True,
            timezone=True,
        )
    _assert_column(columns, "error_stage", sa.String, nullable=True, length=64)
    _assert_column(columns, "error_category", sa.String, nullable=True, length=64)
    _assert_column(columns, "created_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_column(columns, "updated_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_column(columns, "sent_at", sa.DateTime, nullable=True, timezone=True)
    _assert_column(columns, "completed_at", sa.DateTime, nullable=True, timezone=True)
    if monitoring:
        _assert_column(columns, "response_body_json", JSONB, nullable=True)
        for name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "model_calls",
            "usage_reported_calls",
            "usage_missing_calls",
        ):
            _assert_column(columns, name, sa.Integer, nullable=True)
    _assert_primary_key(details, "pk_llm_gateway_decisions")
    _assert_foreign_keys(
        details,
        {
            "fk_llm_gateway_decisions_tenant": (["tenant_id"], "tenants", "CASCADE"),
            "fk_llm_gateway_decisions_cycle": (["cycle_id"], "llm_gateway_control_cycles", "CASCADE"),
            "fk_llm_gateway_decisions_source_event": (["source_event_id"], "llm_gateway_events", "RESTRICT"),
            "fk_llm_gateway_decisions_action_tracking": (["action_tracking_id"], "action_tracking", "SET NULL"),
        },
    )
    _assert_named_unique(details, "uq_llm_gateway_decisions_identity", ["gateway_id", "decision_id"])
    _assert_named_unique(details, "uq_llm_gateway_decisions_source_event", ["source_event_id"])
    _assert_named_unique(details, "uq_llm_gateway_decisions_lease", ["gateway_id", "decision_lease_id"])
    _assert_index(details, "ix_llm_gateway_decisions_due", ["status", "next_attempt_at", "created_at"])
    if activity_capacity:
        _assert_index(
            details,
            "ix_llm_gateway_decisions_activity_capacity",
            ["activity_capacity_key", "activity_capacity_expires_at"],
        )
    _assert_check_tokens(
        database_url,
        "llm_gateway_decisions",
        {
            "ck_llm_gateway_decisions_action": ("call_skill", "wait", "no_op", "stop_hosting"),
            "ck_llm_gateway_decisions_status": (
                "planned",
                "sending",
                "accepted",
                "rejected",
                "retryable_failed",
                "dead_letter",
                "cancelled",
                "manual",
            ),
            **(
                {
                    "ck_llm_gateway_decisions_activity_capacity_complete": (
                        "activity_capacity_key",
                        "activity_capacity_limit > 0",
                        "activity_capacity_expires_at",
                    )
                }
                if activity_capacity
                else {}
            ),
        },
    )


def _assert_skill_calls(database_url: URL, details: dict[str, Any]) -> None:
    columns = details["columns"]
    assert set(columns) == {
        "id",
        "tenant_id",
        "decision_row_id",
        "terminal_event_id",
        "gateway_id",
        "session_id",
        "decision_id",
        "skill_call_id",
        "skill_name",
        "status",
        "failure_category",
        "reason",
        "retryable",
        "effect_status",
        "effect_applied_at",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
    }
    _assert_column(columns, "id", sa.UUID, nullable=False, default="gen_random_uuid()")
    _assert_column(columns, "tenant_id", sa.UUID, nullable=False)
    _assert_column(columns, "decision_row_id", sa.UUID, nullable=False)
    _assert_column(columns, "terminal_event_id", sa.UUID, nullable=True)
    for name in ("gateway_id", "session_id", "decision_id", "skill_call_id", "skill_name"):
        _assert_column(columns, name, sa.String, nullable=False, length=128)
    _assert_column(columns, "status", sa.String, nullable=False, default="'pending'", length=24)
    _assert_column(columns, "failure_category", sa.String, nullable=True, length=32)
    _assert_column(columns, "reason", sa.String, nullable=True, length=256)
    _assert_column(columns, "retryable", sa.Boolean, nullable=True)
    _assert_column(columns, "effect_status", sa.String, nullable=False, default="'not_applicable'", length=24)
    _assert_column(columns, "effect_applied_at", sa.DateTime, nullable=True, timezone=True)
    _assert_column(columns, "created_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_column(columns, "updated_at", sa.DateTime, nullable=False, default="now()", timezone=True)
    _assert_column(columns, "started_at", sa.DateTime, nullable=True, timezone=True)
    _assert_column(columns, "completed_at", sa.DateTime, nullable=True, timezone=True)
    _assert_primary_key(details, "pk_llm_gateway_skill_calls")
    _assert_foreign_keys(
        details,
        {
            "fk_llm_gateway_skill_calls_tenant": (["tenant_id"], "tenants", "CASCADE"),
            "fk_llm_gateway_skill_calls_decision": (["decision_row_id"], "llm_gateway_decisions", "RESTRICT"),
            "fk_llm_gateway_skill_calls_terminal_event": (["terminal_event_id"], "llm_gateway_events", "RESTRICT"),
        },
    )
    _assert_named_unique(details, "uq_llm_gateway_skill_calls_identity", ["gateway_id", "skill_call_id"])
    _assert_named_unique(details, "uq_llm_gateway_skill_calls_terminal_event", ["terminal_event_id"])
    _assert_index(details, "ix_llm_gateway_skill_calls_decision", ["decision_row_id"])
    _assert_index(details, "ix_llm_gateway_skill_calls_status", ["tenant_id", "status", "updated_at"])
    _assert_check_tokens(
        database_url,
        "llm_gateway_skill_calls",
        {
            "ck_llm_gateway_skill_calls_status": (
                "pending",
                "started",
                "succeeded",
                "failed",
                "cancelled",
                "timeout",
                "manual",
            ),
            "ck_llm_gateway_skill_calls_failure_category": (
                "business_rejected",
                "transport_failed",
                "protocol_failed",
                "internal_failed",
            ),
            "ck_llm_gateway_skill_calls_effect_status": (
                "not_applicable",
                "pending",
                "applied",
                "manual",
            ),
        },
    )


def _assert_inbox_schema(
    database_url: URL,
    *,
    processing_index: bool = True,
    hosted_chat: bool = False,
    activity_plan: bool = False,
) -> None:
    snapshot = _inspect_schema(database_url)
    assert snapshot["tables"] >= INBOX_TABLES
    _assert_sessions(database_url, snapshot["details"]["llm_gateway_sessions"])
    _assert_cycles(
        database_url,
        snapshot["details"]["llm_gateway_control_cycles"],
        activity_plan=activity_plan,
    )
    _assert_events(
        database_url,
        snapshot["details"]["llm_gateway_events"],
        processing_index=processing_index,
        hosted_chat=hosted_chat,
    )


def _assert_v2_schema(
    database_url: URL,
    *,
    processing_index: bool = True,
    hosted_chat: bool = False,
    activity_plan: bool = False,
    activity_capacity: bool = False,
    monitoring: bool = False,
) -> None:
    snapshot = _inspect_schema(database_url)
    assert snapshot["tables"] >= V2_TABLES
    _assert_inbox_schema(
        database_url,
        processing_index=processing_index,
        hosted_chat=hosted_chat,
        activity_plan=activity_plan,
    )
    _assert_decisions(
        database_url,
        snapshot["details"]["llm_gateway_decisions"],
        activity_plan=activity_plan,
        activity_capacity=activity_capacity,
        monitoring=monitoring,
    )
    _assert_skill_calls(database_url, snapshot["details"]["llm_gateway_skill_calls"])


def _v2_schema_fingerprint(database_url: URL) -> dict[str, Any]:
    snapshot = _inspect_schema(database_url)
    return {
        table_name: {
            "columns": {
                column_name: (
                    str(column["type"]),
                    bool(column["nullable"]),
                    None if column["default"] is None else str(column["default"]),
                )
                for column_name, column in snapshot["details"][table_name]["columns"].items()
            },
            "constraints": _catalog_constraints(database_url, table_name),
            "indexes": _catalog_indexes(database_url, table_name),
        }
        for table_name in sorted(V2_TABLES)
    }


def _assert_outbox_schema_removed(database_url: URL) -> None:
    snapshot = _inspect_schema(database_url)
    assert OUTBOX_TABLES.isdisjoint(snapshot["tables"])


def _assert_v2_schema_removed(database_url: URL) -> None:
    snapshot = _inspect_schema(database_url)
    assert V2_TABLES.isdisjoint(snapshot["tables"])
    assert "player_memory" in snapshot["tables"]
    rows = _query_mappings(
        database_url,
        sa.text(
            """
            SELECT to_regclass(index_name) AS object_name
            FROM unnest(CAST(:index_names AS text[])) AS index_name
            """
        ),
        {
            "index_names": [
                "ix_llm_gateway_sessions_tenant_status",
                "ix_llm_gateway_cycles_runnable",
                "ix_llm_gateway_events_due",
                "uq_llm_gateway_events_cycle_processing",
                "ix_llm_gateway_decisions_due",
                "ix_llm_gateway_skill_calls_decision",
                "ix_llm_gateway_skill_calls_status",
                "ix_llm_gateway_sessions_liveness",
            ]
        },
    )
    assert all(row["object_name"] is None for row in rows)


def _prepare_revision_007(migration_config: Config, database_url: URL) -> None:
    snapshot = _inspect_schema(database_url)
    if "alembic_version" not in snapshot["tables"]:
        command.upgrade(migration_config, "007")
        return

    current_revision = _current_revision(database_url)
    if current_revision in {"008", "009", "010", "011", "012", "013", "014", "015", "016"}:
        command.downgrade(migration_config, "007")
        return
    if current_revision in {"001", "002", "003", "004", "005", "006", "007"}:
        command.upgrade(migration_config, "007")
        return
    pytest.fail(f"integration database has unsupported revision {current_revision!r}")


def test_database_override_does_not_import_application_config(
    migration_config: Config,
    test_postgres_url: URL,
) -> None:
    class ImportForbiddenConfigModule(ModuleType):
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"src.config must not be accessed when an Alembic database override exists: {name}")

    blocked_config_module = ImportForbiddenConfigModule("src.config")
    with patch.dict(sys.modules, {"src.config": blocked_config_module}):
        command.current(migration_config)

    assert _current_database(test_postgres_url) == test_postgres_url.database


def test_gateway_v2_migration_round_trip(
    migration_config: Config,
    test_postgres_url: URL,
    _mock_settings: Any,
) -> None:
    _mock_settings.postgres_dsn = "postgresql+asyncpg://invalid:invalid@127.0.0.1:1/not_the_test_database"

    _prepare_revision_007(migration_config, test_postgres_url)
    assert _current_database(test_postgres_url) == test_postgres_url.database
    assert _current_revision(test_postgres_url) == "007"
    _assert_v2_schema_removed(test_postgres_url)

    command.upgrade(migration_config, "head")
    assert _current_database(test_postgres_url) == test_postgres_url.database
    assert _current_revision(test_postgres_url) == "016"
    _assert_v2_schema(
        test_postgres_url,
        processing_index=False,
        hosted_chat=True,
        activity_plan=True,
        activity_capacity=True,
        monitoring=True,
    )
    schema_at_016 = _v2_schema_fingerprint(test_postgres_url)

    command.downgrade(migration_config, "013")
    assert _current_revision(test_postgres_url) == "013"
    _assert_v2_schema(
        test_postgres_url,
        processing_index=False,
        hosted_chat=True,
        activity_plan=True,
    )
    schema_at_013 = _v2_schema_fingerprint(test_postgres_url)
    assert schema_at_013 != schema_at_016

    command.downgrade(migration_config, "012")
    assert _current_revision(test_postgres_url) == "012"
    _assert_v2_schema(test_postgres_url, processing_index=False, hosted_chat=True)
    schema_at_012 = _v2_schema_fingerprint(test_postgres_url)
    assert schema_at_012 != schema_at_013

    command.downgrade(migration_config, "010")
    assert _current_revision(test_postgres_url) == "010"
    _assert_v2_schema(test_postgres_url, processing_index=True)
    schema_at_010 = _v2_schema_fingerprint(test_postgres_url)
    assert schema_at_010 != schema_at_012

    command.upgrade(migration_config, "head")
    assert _current_revision(test_postgres_url) == "016"
    _assert_v2_schema(
        test_postgres_url,
        processing_index=False,
        hosted_chat=True,
        activity_plan=True,
        activity_capacity=True,
        monitoring=True,
    )
    assert _v2_schema_fingerprint(test_postgres_url) == schema_at_016

    command.downgrade(migration_config, "009")
    assert _current_revision(test_postgres_url) == "009"
    assert _v2_schema_fingerprint(test_postgres_url) == schema_at_010
    _assert_v2_schema(test_postgres_url)

    command.downgrade(migration_config, "008")
    assert _current_revision(test_postgres_url) == "008"
    _assert_inbox_schema(test_postgres_url)
    _assert_outbox_schema_removed(test_postgres_url)

    command.downgrade(migration_config, "007")
    assert _current_revision(test_postgres_url) == "007"
    _assert_v2_schema_removed(test_postgres_url)

    command.upgrade(migration_config, "008")
    assert _current_revision(test_postgres_url) == "008"
    _assert_inbox_schema(test_postgres_url)
    _assert_outbox_schema_removed(test_postgres_url)
