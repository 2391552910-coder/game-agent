from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

WorkerState = Literal["starting", "running", "draining", "stopped"]


@dataclass(frozen=True)
class WorkerStatusSnapshot:
    state: WorkerState
    heartbeat_monotonic: float | None
    last_successful_poll_monotonic: float | None
    dead_letter_count: int
    degraded: bool


class WorkerStatusRegistry:
    def __init__(
        self,
        monotonic: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[], None] | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._on_state_change = on_state_change
        self._state: WorkerState = "stopped"
        self._heartbeat_monotonic: float | None = None
        self._last_successful_poll_monotonic: float | None = None
        self._dead_letter_count = 0

    def mark_running(self) -> None:
        self._set_state("running")

    def mark_starting(self) -> None:
        self._set_state("starting")

    def mark_draining(self) -> None:
        self._set_state("draining")

    def mark_stopped(self) -> None:
        self._set_state("stopped")

    def set_state_change_callback(self, callback: Callable[[], None] | None) -> None:
        self._on_state_change = callback

    def _set_state(self, state: WorkerState) -> None:
        changed = self._state != state
        self._state = state
        if changed and self._on_state_change is not None:
            self._on_state_change()

    def heartbeat(self) -> None:
        self._heartbeat_monotonic = self._monotonic()

    def mark_successful_poll(self) -> None:
        self._last_successful_poll_monotonic = self._monotonic()

    def set_dead_letter_count(self, count: int) -> None:
        if count < 0:
            raise ValueError("dead letter count must be non-negative")
        self._dead_letter_count = count

    def snapshot(self) -> WorkerStatusSnapshot:
        return WorkerStatusSnapshot(
            state=self._state,
            heartbeat_monotonic=self._heartbeat_monotonic,
            last_successful_poll_monotonic=self._last_successful_poll_monotonic,
            dead_letter_count=self._dead_letter_count,
            degraded=self._dead_letter_count > 0,
        )
