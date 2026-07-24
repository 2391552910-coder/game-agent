from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

_UPDATE_ACTION_TRACKING = sa.text(
    """
    UPDATE action_tracking
    SET status = :tracking_status,
        completed_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = :action_tracking_id
      AND status = 'tracking'
    RETURNING id
    """
)


def tracking_status_for_terminal(terminal_status: str) -> str:
    statuses = {
        "succeeded": "completed",
        "failed": "abandoned",
        "cancelled": "abandoned",
        "timeout": "timeout",
    }
    try:
        return statuses[terminal_status]
    except KeyError:
        raise ValueError("unsupported terminal status") from None


class TerminalEffectService:
    async def apply(
        self,
        session: AsyncSession,
        *,
        action_tracking_id: UUID,
        terminal_status: str,
    ) -> None:
        result = await session.execute(
            _UPDATE_ACTION_TRACKING,
            {
                "action_tracking_id": action_tracking_id,
                "tracking_status": tracking_status_for_terminal(terminal_status),
            },
        )
        if result.scalar_one_or_none() is None:
            raise RuntimeError("action tracking terminal effect was not applied")
