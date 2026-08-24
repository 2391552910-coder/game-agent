from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.agents.gateway_v2_models import GatewayV2AgentContext
from src.core.infrastructure.db import async_session_factory
from src.core.integration.llm_gateway_v2.activity_capacity import (
    DEFAULT_ACTIVITY_CAPACITY_POLICY,
    ActivityCapacityPolicy,
    ActivityCapacitySnapshot,
    scene_id_from_snapshot,
)


class ActivityCapacityUnavailableError(RuntimeError):
    pass


class _SessionFactory(Protocol):
    def __call__(self) -> AsyncSession: ...


_SELECT_ACTIVE_CAPACITY = sa.text(
    """
    SELECT d.activity_capacity_key, count(DISTINCT d.id) AS active_count
    FROM llm_gateway_decisions AS d
    LEFT JOIN llm_gateway_skill_calls AS sc ON sc.decision_row_id = d.id
    WHERE d.activity_capacity_key = ANY(:capacity_keys)
      AND d.activity_capacity_expires_at > clock_timestamp()
      AND (
          d.status IN ('planned', 'sending', 'retryable_failed')
          OR (d.status = 'accepted' AND sc.status IN ('pending', 'started'))
      )
    GROUP BY d.activity_capacity_key
    """
).bindparams(sa.bindparam("capacity_keys", type_=postgresql.ARRAY(sa.String())))


class ActivityCapacityRepository:
    def __init__(
        self,
        session_factory: _SessionFactory | Callable[[], AsyncSession] = async_session_factory,
        *,
        policy: ActivityCapacityPolicy = DEFAULT_ACTIVITY_CAPACITY_POLICY,
    ) -> None:
        self._session_factory = session_factory
        self.policy = policy

    async def snapshot(
        self,
        gateway_id: str,
        context: GatewayV2AgentContext,
    ) -> ActivityCapacitySnapshot:
        key_to_skill = {
            key: rule.skill_name
            for rule in self.policy.rules
            if (key := self.policy.capacity_key(
                gateway_id,
                rule.skill_name,
                context.session_snapshot,
            )) is not None
        }
        if not key_to_skill:
            return ActivityCapacitySnapshot(
                scene_id=scene_id_from_snapshot(context.session_snapshot),
                active_by_skill={},
                policy=self.policy,
            )
        try:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        _SELECT_ACTIVE_CAPACITY,
                        {"capacity_keys": list(key_to_skill)},
                    )
                ).mappings().all()
        except SQLAlchemyError as error:
            raise ActivityCapacityUnavailableError from error
        active_by_skill = {
            key_to_skill[str(row["activity_capacity_key"])]: int(row["active_count"])
            for row in rows
        }
        return ActivityCapacitySnapshot(
            scene_id=scene_id_from_snapshot(context.session_snapshot),
            active_by_skill=active_by_skill,
            policy=self.policy,
        )

    async def occupancy(
        self,
        gateway_id: str,
        context: GatewayV2AgentContext,
    ) -> dict[str, tuple[int, int]]:
        """Return aggregate active/limit pairs for operational reporting."""
        snapshot = await self.snapshot(gateway_id, context)
        return {
            skill_name: (
                snapshot.active_count(skill_name),
                limit,
            )
            for skill_name in {rule.skill_name for rule in self.policy.rules}
            if (
                limit := self.policy.limit_for(
                    skill_name,
                    scene_id=snapshot.scene_id,
                )
            )
            is not None
        }

    @staticmethod
    def capacity_parameters(
        *,
        policy: ActivityCapacityPolicy,
        gateway_id: str,
        skill_name: str,
        session_snapshot: Mapping[str, object],
    ) -> tuple[str | None, int | None]:
        key = policy.capacity_key(gateway_id, skill_name, session_snapshot)
        limit = policy.limit_for(skill_name, scene_id=scene_id_from_snapshot(session_snapshot))
        return key, limit
