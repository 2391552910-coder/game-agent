---
name: myagent2-sgai-http-e2e
description: Use when validating or debugging myAgent2 and SGAI AiRobotGateway v1/v2 bidirectional HTTP integration, including HMAC identity, durable events, decision leases, readiness, recovery, and metrics failures.
---

# myAgent2 SGAI HTTP E2E

## Overview

Run the project-local script from `D:\Projects\myAgent2`. Select the HTTP contract and Gateway implementation independently:

| Contract | Gateway mode | Meaning of success |
|---|---|---|
| `v1` | `Simulation` | Real myAgent2 HTTP plus Mock SGAI validates the existing two-event v1 contract; it does not prove real SGAI |
| `v2` | `Simulation` | Real myAgent2 HTTP/PostgreSQL/Redis plus Mock SGAI validates capabilities, four durable events, two decisions, terminal state, stop, and database hashes; it does not prove real SGAI |
| `v1` | `Real` | Existing SGAI Process 20 smoke flow and v1 metrics |
| `v2` | `Real` | Fixture-configured SGAI Process 20 and the same durable database assertions as v2 Simulation |

Never infer Real v2 Gateway configuration keys. Real v2 requires `tests/fixtures/llm_gateway_v2/gateway_runtime_config_keys.json` exported by the Gateway maintainers and fails before build when it is absent.

## Simulation

Run the preserved v1 simulation with:

```powershell
.\.codex\skills\myagent2-sgai-http-e2e\scripts\Invoke-MyAgent2SgaiHttpE2E.ps1 -ContractVersion v1 -GatewayMode Simulation
```

For v2, first set an isolated PostgreSQL database. The scripts reject any database whose name does not start with `myagent_test_`:

```powershell
$env:TEST_POSTGRES_DSN = 'postgresql+asyncpg://myagent:myagent@127.0.0.1:5432/myagent_test_llm_gateway_v2_e2e'
.\.codex\skills\myagent2-sgai-http-e2e\scripts\Invoke-MyAgent2SgaiHttpE2E.ps1 -ContractVersion v2 -GatewayMode Simulation
```

The script starts Docker Desktop when needed, starts `postgres` and `redis`, migrates and seeds only the guarded test database for v2, runs myAgent2 on port `8000`, and runs Mock SGAI on port `19091`. It restores the original process environment and dependency state during cleanup.

For dependencies already managed elsewhere:

```powershell
.\.codex\skills\myagent2-sgai-http-e2e\scripts\Invoke-MyAgent2SgaiHttpE2E.ps1 -ContractVersion v2 -GatewayMode Simulation -SkipDependencyManagement
```

Keep dependencies running for debugging with `-KeepDependencies`. Replace stale listeners only when intentional with `-StopExistingPorts`.

v2 Simulation passes only when all of these hold:

- `/ready` and `/api/gateway/v2/capabilities` are ready with the exact v2 events path.
- Event, decision, and control requests use three distinct HMAC identities.
- `session_started -> decision -> skill_started -> skill_finished -> decision -> stop -> session_stopped` completes in order.
- Metrics end at four sent events, two accepted decisions, zero failed events, and zero rejected decisions for the isolated simulation.
- PostgreSQL contains one stopped cycle, four succeeded inbox events, two accepted outbox decisions, verified request body hashes, one succeeded call, and one stop-cancelled call.
- Output contains `success=true`, `gatewayMode=Simulation`, and `provesRealSgai=false` without credentials or raw bodies.

## Real SGAI

Run v1 only when the SGAI checkout can build and generated configuration is present:

```powershell
.\.codex\skills\myagent2-sgai-http-e2e\scripts\Invoke-MyAgent2SgaiHttpE2E.ps1 -ContractVersion v1 -GatewayMode Real
```

Real v2 additionally requires process-scoped `E2E_EVENT_APP_ID`, `E2E_EVENT_APP_SECRET`, `E2E_DECISION_APP_ID`, `E2E_DECISION_APP_SECRET`, `E2E_GATEWAY_CONTROL_APP_ID`, `E2E_GATEWAY_CONTROL_APP_SECRET`, and `TEST_POSTGRES_DSN`:

```powershell
.\.codex\skills\myagent2-sgai-http-e2e\scripts\Invoke-MyAgent2SgaiHttpE2E.ps1 -ContractVersion v2 -GatewayMode Real
```

Use `-SkipBuild` only when `D:\Projects\游戏场景数据\SGAI\Bin\App.dll` is current. Real mode requires generated `.bytes` files, a working smoke account and connection profiles, and ports `8000`, `19091`, and `20020`.

Real v2 passes only after readiness, capabilities, two accepted decisions, control stop, stopped database state, and body-hash verification. The current Real v2 run remains blocked while the Gateway runtime fixture or required SGAI build inputs are unavailable; do not report it as verified in that state.

## Results And Failures

Results and service logs are under `.codex\skills\myagent2-sgai-http-e2e\.run`.

| Symptom | Check |
|---|---|
| myAgent2 health/readiness timeout | Test PostgreSQL/Redis addresses, migration revision, worker state, port `8000` |
| `signature_invalid` | Role-specific App ID/secret, signed path, raw JSON bytes, timestamp |
| Events accepted but no decision | Decision URL and deterministic adapter in Simulation; Agent output in Real |
| Decision rejected | Lease ID, `stateVersion`, generation, allowed skill, consumed lease |
| Real SGAI port timeout | Build output, generated config, ports `19091` and `20020` |
| Real v2 stops before build | Gateway runtime fixture or one of the six `E2E_*` identity variables is missing |
| Database safety failure | `TEST_POSTGRES_DSN` does not resolve to a `myagent_test_*` database |

Never treat a `200` from either events endpoint alone as bidirectional success. Never use a fabricated lease for `/api/v1/hosting/llm/decision`, and never claim Real v2 success while the runtime fixture is absent.
