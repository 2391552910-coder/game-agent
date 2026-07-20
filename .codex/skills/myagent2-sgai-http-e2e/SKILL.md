---
name: myagent2-sgai-http-e2e
description: Use when validating or debugging myAgent2 and SGAI AiRobotGateway bidirectional HTTP integration, including events[], HMAC, decision lease, stateVersion, startup, and metrics failures.
---

# myAgent2 SGAI HTTP E2E

## Overview

Run the project-local script from `D:\Projects\myAgent2`. It has two explicit validation levels:

| Mode | Components | Meaning of success |
|---|---|---|
| `Simulation` (default) | Real myAgent2 HTTP, PostgreSQL, Redis, deterministic Agent output, Mock SGAI | Proves the `events[] -> myAgent2 -> /llm/decision` HTTP contract; it does not prove real SGAI |
| `Real` | Real myAgent2, real SGAI Process 20, real account login | Proves the full runtime integration when SGAI accepts decisions in its metrics |

Do not require myAgent2 `GET /capabilities`. SGAI treats capability probing as optional.

## Simulation

Use this first after changing either side of the HTTP contract:

```powershell
.\.codex\skills\myagent2-sgai-http-e2e\scripts\Invoke-MyAgent2SgaiHttpE2E.ps1 -Mode Simulation
```

The script starts Docker Desktop when needed, starts `postgres` and `redis` from `docker-compose.dev.yml`, runs myAgent2 on port `8000`, and runs Mock SGAI on port `19091`. It restores the original Docker/container state during cleanup.

For dependencies already managed elsewhere:

```powershell
.\.codex\skills\myagent2-sgai-http-e2e\scripts\Invoke-MyAgent2SgaiHttpE2E.ps1 -Mode Simulation -SkipDependencyManagement
```

Keep dependencies running for debugging with `-KeepDependencies`. Replace stale listeners only when intentional with `-StopExistingPorts`.

Simulation passes only when all of these hold:

- Signed `account-login-start` and status requests succeed.
- One signed `events[]` request contains `session_started` and `observation_updated`.
- myAgent2 accepts both events and returns both batch results.
- myAgent2 returns two signed `call_skill/observe_state` decisions with matching lease and `stateVersion` values `1` and `2`.
- Metrics equal `llmEventsSent=2`, `llmEventsFailed=0`, `llmDecisionsAccepted=2`, and `llmDecisionsRejected=0`.
- Output contains `success=true`, `mode=Simulation`, and `provesRealSgai=false`.

## Real SGAI

Run only when the SGAI checkout can build and its generated configuration is present:

```powershell
.\.codex\skills\myagent2-sgai-http-e2e\scripts\Invoke-MyAgent2SgaiHttpE2E.ps1 -Mode Real
```

Use `-SkipBuild` only when `D:\Projects\游戏场景数据\SGAI\Bin\App.dll` is already current. Real mode requires:

- Access to SGAI build inputs, including `SG_ExcelData`.
- Generated `.bytes` files under `Config\Excel\cs\GameConfig`.
- A working smoke account and login/game connection profiles.
- Ports `8000`, `19091`, and `20020` available.

The script builds before checking `Bin\App.dll`, starts myAgent2 and SGAI Process 20, calls `account-login-start`, waits for `Running`, and verifies SGAI metrics. Real mode passes only when `llmEventsSent > 0` and `llmDecisionsAccepted > 0`; output then contains `provesRealSgai=true`.

The current local SGAI build remains blocked when `SG_ExcelData` access or generated configuration is unavailable. Do not report Real mode as verified in that state.

## Results And Failures

Results and service logs are under `.codex\skills\myagent2-sgai-http-e2e\.run`.

| Symptom | Check |
|---|---|
| myAgent2 health timeout | `.env` PostgreSQL/Redis addresses, dependency health, port `8000` |
| `signature_invalid` | App ID/secret, signed path, raw JSON bytes, timestamp |
| Events accepted but no decision | decision URL and deterministic wrapper in Simulation; Agent output in Real |
| Decision rejected | lease ID, `stateVersion`, allowed skill, consumed lease |
| Real SGAI port timeout | build output, generated config, ports `19091` and `20020` |

Never treat a `200` from `/api/gateway/events` alone as bidirectional success. Never use a fabricated lease for `/api/v1/hosting/llm/decision`.
