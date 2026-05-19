# RobotGateway 回调实现计划

> **给执行代码的 Agent:** 必须按步骤执行本计划。每一步都用复选框（`- [ ]`）跟踪进度；每个功能点先写测试，再实现，再验证。

**目标：** myAgent2 在玩家分析完成并成功持久化结果后，主动通过 HTTP 回调把分析结果推送给 RobotGateway。

**架构：** 保留当前 RobotGateway → myAgent2 的 Webhook 输入链路；新增 myAgent2 → RobotGateway 的 HTTP callback 输出链路。callback 由 Prefect `analysis_flow` 在 `store_result_task` 成功后触发，发送结构化 JSON payload；RobotGateway 的接收 URL 与鉴权方式尚未最终确定，因此先用配置项预留，未配置 URL 时跳过发送并记录日志，不阻塞现有分析流程。

**技术栈：** Python 3.11+、FastAPI、Prefect、httpx、pydantic-settings、pytest、pytest-asyncio。

---

## 当前通信协议结论

当前项目对外通信主要是 HTTP。

### 1. RobotGateway → myAgent2：HTTP Webhook

RobotGateway 通过 HTTP 请求通知 myAgent2 玩家事件。

- 端点：`POST /webhooks/player-event`
- 认证：请求头 `X-API-Key: <tenant-api-key>`
- 事件类型：`online` / `offline` / `behavior_checkpoint`
- 代码入口：`src/api/routes/webhooks.py`
- 调度入口：`src/core/scheduler/triggers.py`

示例：

```bash
curl -X POST http://localhost:8000/webhooks/player-event \
  -H "Content-Type: application/json" \
  -H "X-API-Key: gap_a1b2c3d4e5f6789012345678" \
  -d '{
    "user_id": "player_12345",
    "event_type": "offline",
    "timestamp": 1744100000.5
  }'
```

这段命令不是 myAgent2 主动发出的，而是 RobotGateway 调用 myAgent2 的 HTTP 服务。

### 2. myAgent2 → RobotGateway：新增 HTTP Callback

当前项目缺少主动把分析结果推给 RobotGateway 的实现。本计划新增该能力：

- 触发时机：`analysis_flow` 中 `store_result_task` 成功之后
- 发送方向：myAgent2 主动 `POST` 到 RobotGateway
- URL 来源：配置项 `ROBOTGATEWAY_CALLBACK_URL`
- 鉴权预留：配置项 `ROBOTGATEWAY_CALLBACK_API_KEY`，发送请求头 `X-Callback-API-Key`
- 事件类型：`analysis.completed`

### 3. 历史分析查询不是 RobotGateway 主链路

`GET /api/v1/analysis/{user_id}/latest` 和 `GET /api/v1/analysis/{user_id}/history` 保留为内部查询、调试或后台管理能力。

RobotGateway 不应通过轮询这些接口获取分析结果；myAgent2 应在分析完成后主动推送结果。

---

## 文件结构

### 新增：`src/core/integration/robotgateway_callback.py`

职责：封装 myAgent2 主动回调 RobotGateway 的全部 HTTP 发送逻辑。

包含：

- `RobotGatewayCallbackSkipped`
- `RobotGatewayCallbackError`
- `build_robotgateway_callback_payload(...)`
- `build_robotgateway_callback_headers(...)`
- `send_robotgateway_analysis_callback(...)`

边界：

- 不依赖 FastAPI。
- 不依赖 Prefect。
- 只接收 `tenant_id`、`user_id`、`snapshot`、`output` 等纯数据。
- 通过调用方传入 callback URL、timeout、鉴权参数。

### 修改：`src/config.py`

职责：新增 RobotGateway callback 配置。

新增字段：

```python
    # ── RobotGateway Callback ──
    robotgateway_callback_url: str | None = Field(default=None)
    robotgateway_callback_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    robotgateway_callback_api_key: str | None = Field(default=None)
```

说明：

- URL 未确定，因此允许为空。
- 初期只预留简单 API Key header：`X-Callback-API-Key`。
- 后续如果 RobotGateway 确认使用 Bearer Token、HMAC 签名或 mTLS，只替换 header 构建逻辑和配置字段，不改 Prefect flow 主流程。

### 修改：`src/core/scheduler/flows/analysis_flow.py`

职责：在 `store_result_task(...)` 成功后新增 callback task。

新增：

- `send_callback_task(tenant_id: str, user_id: str, snapshot: dict, output: dict) -> None`

调用顺序：

1. `run_agent_task(...)`
2. `store_result_task(...)`
3. `send_callback_task(...)`

原则：

- callback 失败不回滚已写入的分析结果。
- callback 失败应让 Prefect task 失败并触发该 task 自身重试，避免静默丢失。
- callback URL 未配置属于明确跳过，不视为失败。

### 新增：`tests/unit/test_robotgateway_callback.py`

职责：单测 payload、headers、跳过逻辑、HTTP 成功与失败逻辑。

### 新增或修改：`tests/unit/test_analysis_flow_callback.py`

职责：验证 `analysis_flow` 在存储结果后调用 callback task。

### 修改：`docs/integration-guide.md`

职责：把直接外部通信对象从“游戏服务器”统一改为“RobotGateway”，并补充 myAgent2 主动回调 RobotGateway 的通信方向。

---

## 第一部分：RobotGateway callback 客户端

### 任务 1：新增 callback 配置

**文件：**

- 修改：`src/config.py`

- [ ] **步骤 1：检查是否已有配置测试**

运行：

```bash
uv run pytest tests/unit -k config -v
```

预期：

- 如果已有 config 单测，记录可复用的测试文件。
- 如果没有 config 单测，不为此单独新增测试，后续由 callback client 单测覆盖配置读取行为。

- [ ] **步骤 2：添加配置字段**

在 `src/config.py` 的调度配置附近添加：

```python
    # ── RobotGateway Callback ──
    robotgateway_callback_url: str | None = Field(default=None)
    robotgateway_callback_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    robotgateway_callback_api_key: str | None = Field(default=None)
```

- [ ] **步骤 3：执行配置导入检查**

运行：

```bash
uv run python - <<'PY'
from src.config import settings
print({
    "robotgateway_callback_url": settings.robotgateway_callback_url,
    "robotgateway_callback_timeout_seconds": settings.robotgateway_callback_timeout_seconds,
    "has_robotgateway_callback_api_key": bool(settings.robotgateway_callback_api_key),
})
PY
```

预期：

- 命令退出码为 0。
- 输出包含三个新增字段。

---

### 任务 2：实现 callback payload 构造函数

**文件：**

- 新增：`src/core/integration/robotgateway_callback.py`
- 新增或修改：`tests/unit/test_robotgateway_callback.py`

- [ ] **步骤 1：写失败测试**

创建 `tests/unit/test_robotgateway_callback.py`，加入：

```python
from src.core.integration.robotgateway_callback import build_robotgateway_callback_payload


def test_build_robotgateway_callback_payload_contains_analysis_result():
    snapshot = {
        "level": 28,
        "profession": "程序员",
        "current_area": "商业区",
    }
    output = {
        "player_profile": {"engagement_level": "high"},
        "recommended_actions": [
            {"action_type": "complete_learning_course", "priority": "high"},
        ],
    }

    payload = build_robotgateway_callback_payload(
        tenant_id="tenant_001",
        user_id="player_001",
        snapshot=snapshot,
        output=output,
    )

    assert payload["event_type"] == "analysis.completed"
    assert payload["tenant_id"] == "tenant_001"
    assert payload["user_id"] == "player_001"
    assert payload["snapshot"] == snapshot
    assert payload["analysis"] == output
    assert isinstance(payload["timestamp"], str)
```

- [ ] **步骤 2：运行测试，确认失败**

运行：

```bash
uv run pytest tests/unit/test_robotgateway_callback.py::test_build_robotgateway_callback_payload_contains_analysis_result -v
```

预期：

- 测试失败，因为 `src.core.integration.robotgateway_callback` 或函数尚未实现。

- [ ] **步骤 3：实现 payload 构造函数**

创建 `src/core/integration/robotgateway_callback.py`：

```python
from datetime import UTC, datetime
from typing import Any


def build_robotgateway_callback_payload(
    *,
    tenant_id: str,
    user_id: str,
    snapshot: dict[str, Any],
    output: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_type": "analysis.completed",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "snapshot": snapshot,
        "analysis": output,
    }
```

- [ ] **步骤 4：运行测试，确认通过**

运行：

```bash
uv run pytest tests/unit/test_robotgateway_callback.py::test_build_robotgateway_callback_payload_contains_analysis_result -v
```

预期：

- 测试通过。

---

### 任务 3：实现 callback headers 构造函数

**文件：**

- 修改：`src/core/integration/robotgateway_callback.py`
- 修改：`tests/unit/test_robotgateway_callback.py`

- [ ] **步骤 1：写失败测试**

在 `tests/unit/test_robotgateway_callback.py` 追加：

```python
from src.core.integration.robotgateway_callback import build_robotgateway_callback_headers


def test_build_robotgateway_callback_headers_without_api_key():
    headers = build_robotgateway_callback_headers(api_key=None)

    assert headers == {"Content-Type": "application/json"}


def test_build_robotgateway_callback_headers_with_api_key():
    headers = build_robotgateway_callback_headers(api_key="secret")

    assert headers == {
        "Content-Type": "application/json",
        "X-Callback-API-Key": "secret",
    }
```

- [ ] **步骤 2：运行测试，确认失败**

运行：

```bash
uv run pytest tests/unit/test_robotgateway_callback.py::test_build_robotgateway_callback_headers_without_api_key tests/unit/test_robotgateway_callback.py::test_build_robotgateway_callback_headers_with_api_key -v
```

预期：

- 测试失败，因为 `build_robotgateway_callback_headers` 尚未实现。

- [ ] **步骤 3：实现 headers 构造函数**

在 `src/core/integration/robotgateway_callback.py` 添加：

```python
def build_robotgateway_callback_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Callback-API-Key"] = api_key
    return headers
```

- [ ] **步骤 4：运行 callback 测试**

运行：

```bash
uv run pytest tests/unit/test_robotgateway_callback.py -v
```

预期：

- 当前 `test_robotgateway_callback.py` 中的测试全部通过。

---

### 任务 4：实现 HTTP callback 发送函数

**文件：**

- 修改：`src/core/integration/robotgateway_callback.py`
- 修改：`tests/unit/test_robotgateway_callback.py`

- [ ] **步骤 1：写 URL 未配置时跳过的失败测试**

在 `tests/unit/test_robotgateway_callback.py` 追加：

```python
import pytest

from src.core.integration.robotgateway_callback import RobotGatewayCallbackSkipped, send_robotgateway_analysis_callback


@pytest.mark.asyncio
async def test_send_robotgateway_analysis_callback_skips_when_url_missing():
    with pytest.raises(RobotGatewayCallbackSkipped):
        await send_robotgateway_analysis_callback(
            callback_url=None,
            api_key=None,
            timeout_seconds=10.0,
            tenant_id="tenant_001",
            user_id="player_001",
            snapshot={},
            output={},
        )
```

- [ ] **步骤 2：写 HTTP 成功发送的失败测试**

继续追加：

```python
import httpx


@pytest.mark.asyncio
async def test_send_robotgateway_analysis_callback_posts_payload():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)

    await send_robotgateway_analysis_callback(
        callback_url="http://robotgateway.local/callbacks/analysis",
        api_key="secret",
        timeout_seconds=10.0,
        tenant_id="tenant_001",
        user_id="player_001",
        snapshot={"level": 28},
        output={"recommended_actions": []},
        transport=transport,
    )

    assert len(requests) == 1
    assert str(requests[0].url) == "http://robotgateway.local/callbacks/analysis"
    assert requests[0].headers["X-Callback-API-Key"] == "secret"
    assert requests[0].headers["Content-Type"] == "application/json"
```

- [ ] **步骤 3：写 HTTP 非 2xx 失败测试**

继续追加：

```python
from src.core.integration.robotgateway_callback import RobotGatewayCallbackError


@pytest.mark.asyncio
async def test_send_robotgateway_analysis_callback_raises_on_non_2xx():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "failed"})

    transport = httpx.MockTransport(handler)

    with pytest.raises(RobotGatewayCallbackError):
        await send_robotgateway_analysis_callback(
            callback_url="http://robotgateway.local/callbacks/analysis",
            api_key=None,
            timeout_seconds=10.0,
            tenant_id="tenant_001",
            user_id="player_001",
            snapshot={},
            output={},
            transport=transport,
        )
```

- [ ] **步骤 4：运行测试，确认失败**

运行：

```bash
uv run pytest tests/unit/test_robotgateway_callback.py -v
```

预期：

- 新增 sender 测试失败，因为 sender 和异常类尚未实现。

- [ ] **步骤 5：实现 sender 和异常类**

在 `src/core/integration/robotgateway_callback.py` 添加：

```python
import httpx


class RobotGatewayCallbackSkipped(Exception):
    pass


class RobotGatewayCallbackError(Exception):
    pass


async def send_robotgateway_analysis_callback(
    *,
    callback_url: str | None,
    api_key: str | None,
    timeout_seconds: float,
    tenant_id: str,
    user_id: str,
    snapshot: dict[str, Any],
    output: dict[str, Any],
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    if not callback_url:
        raise RobotGatewayCallbackSkipped("RobotGateway callback URL is not configured")

    payload = build_robotgateway_callback_payload(
        tenant_id=tenant_id,
        user_id=user_id,
        snapshot=snapshot,
        output=output,
    )
    headers = build_robotgateway_callback_headers(api_key)

    async with httpx.AsyncClient(timeout=timeout_seconds, transport=transport) as client:
        try:
            response = await client.post(callback_url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RobotGatewayCallbackError(str(exc)) from exc
```

- [ ] **步骤 6：运行 callback 测试，确认通过**

运行：

```bash
uv run pytest tests/unit/test_robotgateway_callback.py -v
```

预期：

- `test_robotgateway_callback.py` 全部通过。

---

## 第二部分：接入分析 Flow

### 任务 5：在 analysis flow 中新增 callback task

**文件：**

- 修改：`src/core/scheduler/flows/analysis_flow.py`
- 新增或修改：`tests/unit/test_analysis_flow_callback.py`

- [ ] **步骤 1：写失败测试**

创建或更新 `tests/unit/test_analysis_flow_callback.py`：

```python
import pytest

from src.core.scheduler.flows import analysis_flow as flow_module


@pytest.mark.asyncio
async def test_send_callback_task_calls_robotgateway_callback(monkeypatch):
    calls = []

    async def fake_send_robotgateway_analysis_callback(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        flow_module,
        "send_robotgateway_analysis_callback",
        fake_send_robotgateway_analysis_callback,
        raising=False,
    )
    monkeypatch.setattr(
        flow_module.settings,
        "robotgateway_callback_url",
        "http://robotgateway.local/callbacks/analysis",
        raising=False,
    )
    monkeypatch.setattr(flow_module.settings, "robotgateway_callback_api_key", "secret", raising=False)
    monkeypatch.setattr(flow_module.settings, "robotgateway_callback_timeout_seconds", 10.0, raising=False)

    await flow_module.send_callback_task.fn(
        tenant_id="tenant_001",
        user_id="player_001",
        snapshot={"level": 28},
        output={"recommended_actions": []},
    )

    assert calls == [
        {
            "callback_url": "http://robotgateway.local/callbacks/analysis",
            "api_key": "secret",
            "timeout_seconds": 10.0,
            "tenant_id": "tenant_001",
            "user_id": "player_001",
            "snapshot": {"level": 28},
            "output": {"recommended_actions": []},
        }
    ]
```

- [ ] **步骤 2：运行测试，确认失败**

运行：

```bash
uv run pytest tests/unit/test_analysis_flow_callback.py::test_send_callback_task_calls_robotgateway_callback -v
```

预期：

- 测试失败，因为 `send_callback_task` 尚未实现。

- [ ] **步骤 3：添加 imports**

在 `src/core/scheduler/flows/analysis_flow.py` 顶部添加：

```python
from src.config import settings
from src.core.integration.robotgateway_callback import (
    RobotGatewayCallbackSkipped,
    send_robotgateway_analysis_callback,
)
```

- [ ] **步骤 4：实现 `send_callback_task`**

在 `store_result_task(...)` 后添加：

```python
@task(
    name="send-robotgateway-callback",
    retries=2,
    retry_delay_seconds=30,
    task_run_name="send-robotgateway-callback-{user_id}",
)
async def send_callback_task(tenant_id: str, user_id: str, snapshot: dict, output: dict) -> None:
    logger = get_run_logger()
    try:
        await send_robotgateway_analysis_callback(
            callback_url=settings.robotgateway_callback_url,
            api_key=settings.robotgateway_callback_api_key,
            timeout_seconds=settings.robotgateway_callback_timeout_seconds,
            tenant_id=tenant_id,
            user_id=user_id,
            snapshot=snapshot,
            output=output,
        )
    except RobotGatewayCallbackSkipped as exc:
        logger.info("RobotGateway callback skipped, user_id=%s: %s", user_id, exc)
        return

    logger.info("RobotGateway callback sent, user_id=%s", user_id)
```

- [ ] **步骤 5：运行测试，确认通过**

运行：

```bash
uv run pytest tests/unit/test_analysis_flow_callback.py::test_send_callback_task_calls_robotgateway_callback -v
```

预期：

- 测试通过。

---

### 任务 6：把 callback 接到结果存储之后

**文件：**

- 修改：`src/core/scheduler/flows/analysis_flow.py`
- 修改：`tests/unit/test_analysis_flow_callback.py`

- [ ] **步骤 1：写顺序测试**

在 `tests/unit/test_analysis_flow_callback.py` 追加：

```python
@pytest.mark.asyncio
async def test_analysis_flow_stores_result_before_callback(monkeypatch):
    events = []

    async def fake_run_agent_task(user_id: str, tenant_id: str, snapshot: dict) -> dict:
        events.append("run_agent")
        return {"recommended_actions": []}

    async def fake_store_result_task(tenant_id: str, user_id: str, snapshot: dict, output: dict) -> None:
        events.append("store_result")

    async def fake_send_callback_task(tenant_id: str, user_id: str, snapshot: dict, output: dict) -> None:
        events.append("send_callback")

    monkeypatch.setattr(flow_module.run_agent_task, "fn", fake_run_agent_task)
    monkeypatch.setattr(flow_module.store_result_task, "fn", fake_store_result_task)
    monkeypatch.setattr(flow_module.send_callback_task, "fn", fake_send_callback_task)

    await flow_module.analysis_flow.fn(
        user_id="player_001",
        tenant_id="tenant_001",
        snapshot={"level": 28},
    )

    assert events == ["run_agent", "store_result", "send_callback"]
```

如果 Prefect task wrapper 不允许用 `.fn` 替换执行路径，则改为拆出一个私有编排函数并测试该函数，不启动真实 Prefect worker。

- [ ] **步骤 2：运行测试，确认失败**

运行：

```bash
uv run pytest tests/unit/test_analysis_flow_callback.py::test_analysis_flow_stores_result_before_callback -v
```

预期：

- 测试失败，因为 flow 尚未调用 callback。

- [ ] **步骤 3：更新 flow 调用顺序**

把 `src/core/scheduler/flows/analysis_flow.py` 中：

```python
    output = await run_agent_task(user_id=user_id, tenant_id=tenant_id, snapshot=snapshot)
    await store_result_task(tenant_id=tenant_id, user_id=user_id, snapshot=snapshot, output=output)

    logger.info("分析流程完成, user_id=%s", user_id)
```

改成：

```python
    output = await run_agent_task(user_id=user_id, tenant_id=tenant_id, snapshot=snapshot)
    await store_result_task(tenant_id=tenant_id, user_id=user_id, snapshot=snapshot, output=output)
    await send_callback_task(tenant_id=tenant_id, user_id=user_id, snapshot=snapshot, output=output)

    logger.info("分析流程完成, user_id=%s", user_id)
```

- [ ] **步骤 4：运行 flow callback 测试**

运行：

```bash
uv run pytest tests/unit/test_analysis_flow_callback.py -v
```

预期：

- `test_analysis_flow_callback.py` 全部通过。

---

## 第三部分：文档调整与验证

### 任务 7：更新集成文档中的术语和通信方向

**文件：**

- 修改：`docs/integration-guide.md`

- [ ] **步骤 1：替换直接外部通信对象术语**

把直接与 myAgent2 通信的外部对象从“游戏服务器”改为“RobotGateway”。

保留领域描述中的“玩家”“游戏数据”等词，不需要全部替换。

- [ ] **步骤 2：补充通信方向说明**

在集成文档中明确写出两个方向：

```markdown
## 通信方向

### RobotGateway → myAgent2

RobotGateway 通过 HTTP Webhook 向 myAgent2 发送玩家事件：

- `POST /webhooks/player-event`
- Header: `X-API-Key: <tenant-api-key>`
- Body: `online` / `offline` / `behavior_checkpoint`

### myAgent2 → RobotGateway

myAgent2 在分析完成并写入本地结果后，主动 HTTP POST 回调 RobotGateway：

- URL: 由 `ROBOTGATEWAY_CALLBACK_URL` 配置
- Header: `X-Callback-API-Key`，仅在 `ROBOTGATEWAY_CALLBACK_API_KEY` 配置后发送
- Body event_type: `analysis.completed`
```

- [ ] **步骤 3：移除 RobotGateway 主动拉取分析结果的描述**

不要把 `GET /api/v1/analysis/{user_id}/latest` 或 `GET /api/v1/analysis/{user_id}/history` 描述成 RobotGateway 获取分析结果的主链路。

如果保留这些接口，只标注为“内部查询 / 调试 / 后台管理接口”。

---

### 任务 8：执行验证命令

**文件：**

- 所有本计划涉及的修改文件

- [ ] **步骤 1：运行聚焦单测**

运行：

```bash
uv run pytest tests/unit/test_robotgateway_callback.py tests/unit/test_analysis_flow_callback.py -v
```

预期：

- 所有选中测试通过。

- [ ] **步骤 2：运行相关既有测试**

运行：

```bash
uv run pytest tests/unit/test_robotgateway_callback.py tests/unit/test_analysis_flow_callback.py tests/unit/test_orchestrator.py -v
```

预期：

- 所有选中测试通过。

如已有 webhook、scheduler 或 analysis flow 相关单测，再运行：

```bash
uv run pytest tests/unit -k "callback or webhook or analysis_flow or orchestrator" -v
```

- [ ] **步骤 3：运行 Ruff 检查**

运行：

```bash
uv run ruff check src/core/integration/robotgateway_callback.py src/core/scheduler/flows/analysis_flow.py src/config.py tests/unit/test_robotgateway_callback.py tests/unit/test_analysis_flow_callback.py
```

预期：

- 命令退出码为 0。

- [ ] **步骤 4：运行格式化**

运行：

```bash
uv run ruff format src/core/integration/robotgateway_callback.py src/core/scheduler/flows/analysis_flow.py src/config.py tests/unit/test_robotgateway_callback.py tests/unit/test_analysis_flow_callback.py
```

预期：

- 命令退出码为 0。

---

## 验收标准

- [ ] RobotGateway 仍可通过 `POST /webhooks/player-event` 通知 myAgent2 玩家事件。
- [ ] `analysis_flow` 在 `store_result_task` 成功后调用 `send_callback_task`。
- [ ] `send_callback_task` 使用 `ROBOTGATEWAY_CALLBACK_URL` 主动 POST 分析结果。
- [ ] 未配置 `ROBOTGATEWAY_CALLBACK_URL` 时，分析流程不失败，只记录 callback skipped。
- [ ] 配置 `ROBOTGATEWAY_CALLBACK_API_KEY` 时，请求头包含 `X-Callback-API-Key`。
- [ ] RobotGateway 返回非 2xx 或网络异常时，callback task 失败并交给 Prefect 重试。
- [ ] 文档中的直接外部通信对象统一为 RobotGateway。
- [ ] 不再把 `GET /api/v1/analysis/{user_id}/history` 描述为 RobotGateway 获取分析结果的主路径。

---

## 实现注意事项

- 不要把 RobotGateway callback 写进 FastAPI route；这是分析完成后的后台输出动作，应属于 scheduler flow。
- 不要让 RobotGateway 主动查询历史结果来完成集成；agent 内部可以读取历史，外部集成主链路应是 callback。
- 不要在 URL 未配置时抛出通用错误；这是开发环境和未接入 RobotGateway 时的正常状态。
- 不要吞掉已配置 URL 后的 callback 发送失败；这代表外部交付失败，应让 Prefect 看到失败并重试。
- 鉴权方式未定时，只保留最小 API Key header；后续若确认 HMAC、Bearer、mTLS，只替换 header 构建函数和配置字段，不改 flow 编排。
