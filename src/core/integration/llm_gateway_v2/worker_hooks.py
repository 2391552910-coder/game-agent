from __future__ import annotations

from typing import Protocol


class WorkerHooks(Protocol):
    async def after_event_commit(self, event_ids: tuple[str, ...]) -> None: ...

    async def before_agent(self, event_id: str) -> None: ...

    async def before_decision_http(self, decision_id: str) -> None: ...

    async def after_decision_http(self, decision_id: str) -> None: ...


class NoOpWorkerHooks:
    async def after_event_commit(self, event_ids: tuple[str, ...]) -> None:
        del event_ids

    async def before_agent(self, event_id: str) -> None:
        del event_id

    async def before_decision_http(self, decision_id: str) -> None:
        del decision_id

    async def after_decision_http(self, decision_id: str) -> None:
        del decision_id


NO_OP_WORKER_HOOKS: WorkerHooks = NoOpWorkerHooks()
