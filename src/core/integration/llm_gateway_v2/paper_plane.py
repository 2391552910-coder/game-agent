from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

PAPER_PLANE_NAMES: tuple[str, ...] = ("初级", "中级", "高级")
PAPER_PLANE_DURATION_RANGES_MS: dict[str, tuple[int, int]] = {
    "初级": (100_000, 200_000),
    "中级": (90_000, 180_000),
    "高级": (70_000, 130_000),
}


def build_paper_plane_arguments(*, seed: str) -> dict[str, Any]:
    """Build stable, Gateway-compatible paper-plane arguments for one decision."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    name = PAPER_PLANE_NAMES[digest[0] % len(PAPER_PLANE_NAMES)]
    minimum, maximum = PAPER_PLANE_DURATION_RANGES_MS[name]
    duration = minimum + int.from_bytes(digest[1:9], "big") % (maximum - minimum + 1)
    return {
        "planeName": name,
        "useTimeMs": duration,
        "isComplete": True,
    }


def paper_plane_arguments_seed(
    *,
    session_id: str,
    event_id: str,
    account_id: str | None,
    control_generation: int,
    event_sequence: int,
    decision_lease_id: str,
    state_version: int,
) -> str:
    values = (
        session_id,
        event_id,
        account_id or "",
        str(control_generation),
        str(event_sequence),
        decision_lease_id,
        str(state_version),
    )
    return "paper-plane:" + ":".join(values)


def is_valid_paper_plane_arguments(value: Mapping[str, Any]) -> bool:
    if set(value) != {"planeName", "useTimeMs", "isComplete"}:
        return False
    name = value["planeName"]
    duration = value["useTimeMs"]
    if name not in PAPER_PLANE_DURATION_RANGES_MS:
        return False
    if not isinstance(duration, int) or isinstance(duration, bool):
        return False
    minimum, maximum = PAPER_PLANE_DURATION_RANGES_MS[name]
    return minimum <= duration <= maximum and value["isComplete"] is True
