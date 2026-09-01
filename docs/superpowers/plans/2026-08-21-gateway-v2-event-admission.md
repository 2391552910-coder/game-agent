# Gateway V2 Event Admission Implementation Plan

> **For agentic workers:** REQUIRED: Use the repository's test-first workflow to implement this plan.

**Goal:** Keep `POST /api/gateway/v2/events` fast and reliable for high-concurrency Gateway delivery by bounding database admission work, returning explicit overload responses, and preserving durable ACK and idempotency semantics.

**Architecture:** PostgreSQL remains the authoritative V2 inbox. The HTTP handler authenticates and validates the envelope, then admits the batch through a per-process bounded gate; only a committed database transaction produces a successful ACK. Saturated admission or a bounded database-pool wait maps to a fast `503`, while EventWorker continues all model and callback work asynchronously.

**Tech Stack:** FastAPI, SQLAlchemy async PostgreSQL engine, asyncio semaphore, Pydantic settings, pytest/pytest-asyncio, Alembic only if the optimized SQL requires schema changes.

---

### Task 1: Admission capacity and timeout contract

**Files:**
- Modify: `src/config.py`
- Modify: `src/core/infrastructure/db.py`
- Modify: `src/core/integration/llm_gateway_v2/inbox_repository.py`
- Modify: `src/core/integration/llm_gateway_v2/runtime_metrics.py`
- Test: `tests/unit/llm_gateway_v2/test_event_service.py`
- Test: `tests/api/test_gateway_v2.py`

- [x] Write tests for bounded admission, fast overload, and database pool settings.
- [x] Run the focused tests and confirm they fail for the missing behavior.
- [x] Add configurable pool timeout and process-local admission capacity.
- [x] Map admission saturation to `EventServiceUnavailable` and HTTP `503` without invoking Agent/LLM work.
- [x] Record admission accepted, overloaded, failed, and latency metrics without logging credentials or payloads.
- [x] Run focused tests and confirm they pass.

### Task 2: Efficient durable inbox admission

**Files:**
- Modify: `src/core/integration/llm_gateway_v2/inbox_repository.py`
- Test: `tests/integration/llm_gateway_v2/test_inbox_repository.py`
- Test: `tests/unit/llm_gateway_v2/test_event_service.py`

- [x] Add a failing regression covering a new session/event admission under concurrent requests.
- [x] Reduce unnecessary database round trips while retaining session/cycle creation, event hash conflict detection, duplicate ACKs, and transaction rollback behavior.
- [x] Keep event admission atomic: no successful ACK before commit, and no partial batch visible after failure.
- [x] Run the integration regression against an isolated PostgreSQL fixture.

### Task 3: Observability and API behavior

**Files:**
- Modify: `src/api/routes/gateway_v2.py`
- Modify: `src/core/integration/llm_gateway_v2/event_service.py`
- Modify: `src/core/integration/llm_gateway_v2/runtime_metrics.py`
- Test: `tests/api/test_gateway_v2.py`
- Test: `tests/unit/llm_gateway_v2/test_runtime_metrics.py`

- [x] Verify ACK latency logs include admission outcome and elapsed time.
- [x] Verify overload returns stable `503 service_unavailable` and normal responses remain the existing V2 ACK shape.
- [x] Verify duplicate event IDs remain idempotent and changed content remains `409 event_content_conflict`.

### Task 4: Verification and runtime smoke test

**Files:**
- No production file changes expected.

- [x] Run focused V2 tests.
- [x] Run full unit tests, V2 integration tests, and Ruff for modified files.
- [x] Apply migrations only if a new revision is actually required.
- [ ] Restart the local API on `0.0.0.0:8000` and verify `/health`, `/ready`, `/api/gateway/v2/capabilities`, and one signed V2 event ACK.
- [x] Report any remaining load-test limitation separately from code-level verification.

### Task 5: Constant-round-trip batch admission

**Files:**
- Modify: `src/core/integration/llm_gateway_v2/inbox_repository.py`
- Test: `tests/integration/test_gateway_v2_recovery.py`

- [ ] Add a failing regression proving a 50-event non-chat batch uses a constant number of database statements instead of one savepoint and insert per event.
- [ ] Add failing regressions for concurrent duplicate batches, content conflicts, sequence conflicts, and whole-batch rollback.
- [ ] Replace per-event non-chat insertion with one set-based PostgreSQL admission statement plus one post-insert classification query.
- [ ] Preserve input-order ACKs, `eventId` idempotency, content-hash conflict detection, session/cycle reuse, and commit-before-ACK semantics.
- [ ] Run the focused PostgreSQL integration tests and confirm all batch regressions pass.

### Task 6: Dedicated admission pool and database-owned timeout

**Files:**
- Modify: `src/config.py`
- Modify: `.env.example`
- Modify: `src/core/infrastructure/db.py`
- Modify: `src/api/main.py`
- Modify: `src/core/integration/llm_gateway_v2/event_service.py`
- Modify: `src/core/integration/llm_gateway_v2/inbox_repository.py`
- Test: `tests/conftest.py`
- Test: `tests/unit/llm_gateway_v2/test_event_service.py`
- Test: `tests/integration/test_gateway_v2_recovery.py`

- [ ] Add failing tests proving HTTP event admission uses a session factory separate from EventWorker and DecisionWorker.
- [ ] Add failing tests proving statement timeout maps to fast `503` and the same pool remains usable afterward.
- [ ] Add a dedicated PostgreSQL engine/session factory for event admission with bounded pool size, overflow, and pool wait.
- [ ] Apply PostgreSQL `SET LOCAL statement_timeout` inside the admission transaction and remove the outer `asyncio.timeout()` cancellation boundary.
- [ ] Dispose both engines during application shutdown and expose only sanitized timeout/overload outcomes.
- [ ] Run focused API, unit, PostgreSQL integration, migration, and full unit test suites.

### Task 7: Runtime verification and operational record

**Files:**
- Create: `C:/Users/Admin/Desktop/技术栈/经验/2026-08-21-Gateway-V2大批量事件入库超时修复.md`

- [ ] Restart Uvicorn on `0.0.0.0:8000` without reusing stale environment variables.
- [ ] Verify `/health`, `/ready`, `/api/gateway/v2/capabilities`, and a signed 50-event V2 batch.
- [ ] Confirm logs contain ACK latency, batch size, admission outcome, and no asyncpg cancellation/connection-close errors.
- [ ] Record the report evidence, root cause, implementation, verification results, deployment parameters, and remaining requirement for a real 1000-account 900-second Gateway retest.
