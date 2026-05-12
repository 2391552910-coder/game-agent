# 动态决策系统 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 LangGraph Agent 基础上扩展意图预判 + 动态决策闭环，通过积累玩家长期记忆，实现离线触发后的目标校验、代价权衡和动态决策调整。

**Architecture:** 新增 3 张数据库表（session_events、player_intent、player_memory）存储在线行为序列和玩家长期记忆；在现有 7 节点 LangGraph 图中插入 2 个新节点（intent_inference、goal_evaluation），末尾追加 1 个新节点（memory_update）；Webhook 新增 behavior 端点接收在线期间行为数据。

**Tech Stack:** Python 3.11, FastAPI, LangGraph, SQLAlchemy 2.0 async, Alembic, PostgreSQL, Pydantic v2

---

## 文件结构

### 新建文件

| 文件 | 职责 |
|------|------|
| `alembic/versions/005_session_events.py` | 创建 session_events 表 |
| `alembic/versions/006_player_intent.py` | 创建 player_intent 表 |
| `alembic/versions/007_player_memory.py` | 创建 player_memory 表 |
| `src/core/agents/decision_nodes.py` | intent_inference、goal_evaluation、memory_update 三个新节点 |
| `src/core/agents/decision_prompts.py` | 新节点使用的 prompt 模板 |
| `src/core/agents/decision_models.py` | IntentResult、GoalEvaluationResult、PlayerMemory Pydantic 模型 |
| `tests/unit/test_decision_nodes.py` | 新节点单元测试 |
| `tests/api/test_routes_behavior.py` | behavior Webhook 端点测试 |

### 修改文件

| 文件 | 修改内容 |
|------|---------|
| `src/core/agents/state.py` | 新增 intent_result、goal_evaluation_result、player_memory 字段 |
| `src/core/agents/orchestrator.py` | 注册 3 个新节点，调整图边顺序 |
| `src/api/routes/webhooks.py` | 新增 behavior_checkpoint 事件类型处理 |

---

## Chunk 1: 数据库迁移

### Task 1: session_events 表

**Files:**
- Create: `alembic/versions/005_session_events.py`

存储玩家在线期间游戏服务器推送的行为事件序列。每条记录对应一个行为事件，按 user_id + session_id 组织。

- [ ] **Step 1: 创建迁移文件**

```python
# alembic/versions/005_session_events.py
"""session_events 表

Revision ID: 005
Revises: 004
Create Date: 2026-05-11

新增 session_events 表，用于存储玩家在线期间的行为事件序列：
- 按 user_id + session_id 组织，一次在线期间为一个 session
- 离线触发分析时读取最近一次 session 的事件作为意图推断输入
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "session_events",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            comment="主键",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False, comment="租户 ID"),
        sa.Column("user_id", sa.String(255), nullable=False, comment="玩家 ID"),
        sa.Column(
            "session_id",
            sa.String(64),
            nullable=False,
            comment="会话 ID，同一次在线期间共享同一个 session_id",
        ),
        sa.Column(
            "event_type",
            sa.String(100),
            nullable=False,
            comment="事件类型，如 move / interact / purchase / quest_accept 等",
        ),
        sa.Column(
            "event_data",
            sa.JSON(),
            nullable=True,
            comment="事件详细数据，JSON 格式，内容由游戏服务器决定",
        ),
        sa.Column(
            "snapshot",
            sa.JSON(),
            nullable=True,
            comment="事件发生时的玩家快照（可选，游戏服务器按需附带）",
        ),
        sa.Column(
            "occurred_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="事件发生时间",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="记录创建时间",
        ),
    )

    op.create_index(
        "ix_session_events_tenant_user_session",
        "session_events",
        ["tenant_id", "user_id", "session_id"],
    )
    op.create_index(
        "ix_session_events_occurred_at",
        "session_events",
        ["tenant_id", "user_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_session_events_occurred_at", table_name="session_events")
    op.drop_index("ix_session_events_tenant_user_session", table_name="session_events")
    op.drop_table("session_events")
```

- [ ] **Step 2: 运行迁移**

```bash
uv run alembic upgrade 005
```

期望输出：`Running upgrade 004 -> 005`

- [ ] **Step 3: 验证表结构**

```bash
uv run python -c "
import asyncio
from src.core.infrastructure.db import get_session
from sqlalchemy import text

async def check():
    async with get_session() as s:
        r = await s.execute(text(\"SELECT column_name FROM information_schema.columns WHERE table_name='session_events' ORDER BY ordinal_position\"))
        print([row[0] for row in r.fetchall()])

asyncio.run(check())
"
```

期望输出：`['id', 'tenant_id', 'user_id', 'session_id', 'event_type', 'event_data', 'snapshot', 'occurred_at', 'created_at']`

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/005_session_events.py
git commit -m "feat: 新增 session_events 表迁移"
```

---

### Task 2: player_intent 表

**Files:**
- Create: `alembic/versions/006_player_intent.py`

存储每次离线分析后的意图推断结果和当前目标状态。每个玩家保留最近 N 条记录，历史记录不删除用于趋势分析。

- [ ] **Step 1: 创建迁移文件**

```python
# alembic/versions/006_player_intent.py
"""player_intent 表

Revision ID: 006
Revises: 005
Create Date: 2026-05-11

新增 player_intent 表，用于存储每次离线分析的意图推断结果：
- inferred_intent: 本次会话意图推断（完成了什么/放弃了什么/下次想做什么）
- current_goal: 当前正在追求的主目标
- goal_status: 目标状态（active / completed / abandoned / switched）
- goal_progress: 目标完成度（0.0 - 1.0）
- cost_actual / cost_expected: 实际代价 vs 预期代价
- evaluation_result: goal_evaluation 节点的决策结论（continue / downgrade / switch）
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_intent",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            comment="主键",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False, comment="租户 ID"),
        sa.Column("user_id", sa.String(255), nullable=False, comment="玩家 ID"),
        sa.Column(
            "session_id",
            sa.String(64),
            nullable=True,
            comment="关联的会话 ID",
        ),
        sa.Column(
            "inferred_intent",
            sa.JSON(),
            nullable=True,
            comment=(
                "意图推断结果，JSON 格式，包含："
                "completed（完成了什么）、abandoned（放弃了什么）、next（下次想做什么）"
            ),
        ),
        sa.Column(
            "current_goal",
            sa.Text(),
            nullable=True,
            comment="当前主目标描述",
        ),
        sa.Column(
            "goal_type",
            sa.String(100),
            nullable=True,
            comment="目标分类标签，用于记忆系统统计成功率",
        ),
        sa.Column(
            "goal_status",
            sa.String(20),
            nullable=False,
            server_default="active",
            comment="目标状态: active / completed / abandoned / switched",
        ),
        sa.Column(
            "goal_progress",
            sa.Float(),
            nullable=True,
            comment="目标完成度，0.0 - 1.0",
        ),
        sa.Column(
            "cost_expected",
            sa.Float(),
            nullable=True,
            comment="预期代价（金币/时间等，由上次决策估算）",
        ),
        sa.Column(
            "cost_actual",
            sa.Float(),
            nullable=True,
            comment="实际代价（本次离线时统计）",
        ),
        sa.Column(
            "evaluation_result",
            sa.String(20),
            nullable=True,
            comment="代价权衡决策结论: continue / downgrade / switch",
        ),
        sa.Column(
            "evaluation_reason",
            sa.Text(),
            nullable=True,
            comment="决策原因说明",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="创建时间（对应本次离线分析时间）",
        ),
    )

    op.create_index(
        "ix_player_intent_tenant_user",
        "player_intent",
        ["tenant_id", "user_id"],
    )
    op.create_index(
        "ix_player_intent_created_at",
        "player_intent",
        ["tenant_id", "user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_player_intent_created_at", table_name="player_intent")
    op.drop_index("ix_player_intent_tenant_user", table_name="player_intent")
    op.drop_table("player_intent")
```

- [ ] **Step 2: 运行迁移**

```bash
uv run alembic upgrade 006
```

期望输出：`Running upgrade 005 -> 006`

- [ ] **Step 3: Commit**

```bash
git add alembic/versions/006_player_intent.py
git commit -m "feat: 新增 player_intent 表迁移"
```

---

### Task 3: player_memory 表

**Files:**
- Create: `alembic/versions/007_player_memory.py`

存储玩家长期记忆，每个玩家只有一条记录（upsert 模式），随每次离线分析增量更新。

- [ ] **Step 1: 创建迁移文件**

```python
# alembic/versions/007_player_memory.py
"""player_memory 表

Revision ID: 007
Revises: 006
Create Date: 2026-05-11

新增 player_memory 表，用于存储玩家长期记忆（每个玩家一条记录，upsert 模式）：
- behavior_profile: 行为画像（消费习惯、活跃偏好、在线时长等），每次更新
- goal_history: 目标历史统计（分类统计成功率、平均代价等），目标出现 2 次后写入
- analysis_count: 累计分析次数，用于计算统计均值
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_memory",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            comment="主键",
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False, comment="租户 ID"),
        sa.Column("user_id", sa.String(255), nullable=False, comment="玩家 ID"),
        sa.Column(
            "behavior_profile",
            sa.JSON(),
            nullable=True,
            comment=(
                "行为画像，JSON 格式，包含："
                "spend_tendency（消费倾向：high/medium/low）、"
                "avg_spend_per_session（每次会话平均消费）、"
                "preferred_content（偏好内容类型列表）、"
                "avg_session_minutes（平均在线时长分钟数）、"
                "active_hours（活跃时段列表）"
            ),
        ),
        sa.Column(
            "goal_history",
            sa.JSON(),
            nullable=True,
            comment=(
                "目标历史统计，JSON 格式，以 goal_type 为键，值包含："
                "total（总次数）、success（成功次数）、"
                "avg_cost（平均实际代价）、abandon_reasons（放弃原因列表）"
            ),
        ),
        sa.Column(
            "analysis_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="累计分析次数",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="首次创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="最后更新时间",
        ),
    )

    # 每个玩家在每个租户下只有一条记录
    op.create_unique_constraint(
        "uq_player_memory_tenant_user",
        "player_memory",
        ["tenant_id", "user_id"],
    )
    op.create_index(
        "ix_player_memory_tenant_user",
        "player_memory",
        ["tenant_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_player_memory_tenant_user", table_name="player_memory")
    op.drop_constraint("uq_player_memory_tenant_user", "player_memory", type_="unique")
    op.drop_table("player_memory")
```

- [ ] **Step 2: 运行迁移**

```bash
uv run alembic upgrade 007
```

期望输出：`Running upgrade 006 -> 007`

- [ ] **Step 3: Commit**

```bash
git add alembic/versions/007_player_memory.py
git commit -m "feat: 新增 player_memory 表迁移"
```

---

## Chunk 2: Webhook 行为数据端点

### Task 4: behavior_checkpoint 事件类型

**Files:**
- Modify: `src/api/routes/webhooks.py`
- Create: `tests/api/test_routes_behavior.py`

在现有 Webhook 端点新增 `behavior_checkpoint` 事件类型，接收玩家在线期间的行为数据，写入 `session_events` 表，不触发分析。

- [ ] **Step 1: 写失败测试**

```python
# tests/api/test_routes_behavior.py
"""behavior_checkpoint Webhook 端点测试"""

import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_behavior_checkpoint_writes_to_db(client: AsyncClient, auth_headers: dict):
    """behavior_checkpoint 事件写入 session_events 表"""
    payload = {
        "user_id": "user-001",
        "event_type": "behavior_checkpoint",
        "timestamp": 1700000000.0,
        "session_id": "session-abc",
        "behavior_event": {
            "type": "move",
            "data": {"from": "广场", "to": "咖啡馆"},
        },
        "snapshot": {"level": 10, "gold": 500},
    }

    with patch(
        "src.api.routes.webhooks._write_behavior_event",
        new_callable=AsyncMock,
        return_value=None,
    ) as mock_write:
        response = await client.post(
            "/api/v1/webhooks/player-event",
            json=payload,
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.json()["status"] == "recorded"
    mock_write.assert_called_once()


@pytest.mark.asyncio
async def test_behavior_checkpoint_missing_session_id(client: AsyncClient, auth_headers: dict):
    """behavior_checkpoint 缺少 session_id 时返回 400"""
    payload = {
        "user_id": "user-001",
        "event_type": "behavior_checkpoint",
        "timestamp": 1700000000.0,
        # 缺少 session_id
        "behavior_event": {"type": "move", "data": {}},
    }
    response = await client.post(
        "/api/v1/webhooks/player-event",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_unknown_event_type_still_returns_400(client: AsyncClient, auth_headers: dict):
    """未知事件类型仍返回 400"""
    payload = {
        "user_id": "user-001",
        "event_type": "unknown_type",
        "timestamp": 1700000000.0,
    }
    response = await client.post(
        "/api/v1/webhooks/player-event",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 400
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
uv run pytest tests/api/test_routes_behavior.py -v
```

期望：FAILED（`_write_behavior_event` 不存在）

- [ ] **Step 3: 实现 behavior_checkpoint 端点**

修改 `src/api/routes/webhooks.py`，在现有 `PlayerEvent` 模型添加可选字段，并增加 `_write_behavior_event` 函数和对应的事件处理分支：

```python
# src/api/routes/webhooks.py
"""
游戏服务器 Webhook 端点。

接收玩家在线/离线事件，触发或取消分析流程。
接收玩家在线期间的行为事件，写入 session_events 表。
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


class PlayerEvent(BaseModel):
    user_id: str = Field(..., description="玩家 ID")
    event_type: str = Field(..., description="事件类型: online / offline / behavior_checkpoint")
    timestamp: float = Field(..., description="事件时间戳")
    snapshot: dict | None = Field(default=None, description="玩家快照数据（可选）")
    # behavior_checkpoint 专用字段
    session_id: str | None = Field(default=None, description="会话 ID（behavior_checkpoint 必填）")
    behavior_event: dict | None = Field(default=None, description="行为事件详情")


@router.post("/player-event")
async def handle_player_event(event: PlayerEvent, request: Request):
    """处理玩家在线/离线/行为事件。"""
    tenant_id = request.state.tenant_id

    if event.event_type == "offline":
        from src.core.scheduler.triggers import schedule_offline_analysis

        run_id = await schedule_offline_analysis(
            user_id=event.user_id,
            tenant_id=tenant_id,
            snapshot=event.snapshot,
        )
        if run_id is None:
            return {"status": "debounced", "user_id": event.user_id}
        return {"status": "scheduled", "user_id": event.user_id, "flow_run_id": run_id}

    elif event.event_type == "online":
        from src.core.scheduler.triggers import cancel_offline_analysis

        await cancel_offline_analysis(user_id=event.user_id)
        return {"status": "cancelled", "user_id": event.user_id}

    elif event.event_type == "behavior_checkpoint":
        if not event.session_id:
            raise HTTPException(status_code=422, detail="behavior_checkpoint 事件必须提供 session_id")

        await _write_behavior_event(
            tenant_id=tenant_id,
            user_id=event.user_id,
            session_id=event.session_id,
            behavior_event=event.behavior_event or {},
            snapshot=event.snapshot,
        )
        return {"status": "recorded", "user_id": event.user_id}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"未知事件类型: {event.event_type}",
        )


async def _write_behavior_event(
    tenant_id: str,
    user_id: str,
    session_id: str,
    behavior_event: dict,
    snapshot: dict | None,
) -> None:
    """将行为事件写入 session_events 表。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    event_type = behavior_event.get("type", "unknown")
    event_data = behavior_event.get("data")

    async with get_session() as session:
        await session.execute(
            text("""
                INSERT INTO session_events (
                    tenant_id, user_id, session_id,
                    event_type, event_data, snapshot
                ) VALUES (
                    :tenant_id, :user_id, :session_id,
                    :event_type, :event_data, :snapshot
                )
            """),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "session_id": session_id,
                "event_type": event_type,
                "event_data": event_data,
                "snapshot": snapshot,
            },
        )
    logger.debug(
        "[webhook] 行为事件已写入, user_id=%s, session_id=%s, type=%s",
        user_id,
        session_id,
        event_type,
    )
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
uv run pytest tests/api/test_routes_behavior.py -v
```

期望：全部 PASSED

- [ ] **Step 5: 回归测试原有 Webhook 测试**

```bash
uv run pytest tests/api/test_routes_webhooks.py -v
```

期望：全部 PASSED

- [ ] **Step 6: Commit**

```bash
git add src/api/routes/webhooks.py tests/api/test_routes_behavior.py
git commit -m "feat: Webhook 新增 behavior_checkpoint 事件类型，写入 session_events"
```

---

## Chunk 3: 数据模型与 State 扩展

### Task 5: 新增 Pydantic 模型

**Files:**
- Create: `src/core/agents/decision_models.py`

定义新节点使用的输入输出数据模型。

- [ ] **Step 1: 创建模型文件**

```python
# src/core/agents/decision_models.py
"""动态决策系统数据模型。

intent_inference、goal_evaluation、memory_update 节点的输入输出模型。
"""

from typing import Literal

from pydantic import BaseModel, Field


class InferredIntent(BaseModel):
    """本次会话意图推断结果。"""
    completed: list[str] = Field(
        default_factory=list,
        description="本次会话完成了的事情，如 ['完成了主线任务第三章', '购买了装备']",
    )
    abandoned: list[str] = Field(
        default_factory=list,
        description="本次会话中途放弃的事情，如 ['尝试了 PVP 但中途退出']",
    )
    next_likely: list[str] = Field(
        default_factory=list,
        description="下次上线最可能想做的事情（按可能性排序），如 ['继续主线任务', '强化装备']",
    )
    intent_confidence: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="意图推断的置信度，取决于行为序列的完整性和明确程度",
    )
    session_summary: str = Field(
        default="",
        description="本次会话的简短自然语言总结，供后续节点使用",
    )


class GoalEvaluationResult(BaseModel):
    """目标校验与决策结论。"""
    has_active_goal: bool = Field(
        description="是否有正在进行的目标（来自上次分析的 player_intent 记录）",
    )
    goal_progress: float | None = Field(
        default=None,
        description="当前目标完成度，0.0 - 1.0，无历史目标时为 None",
    )
    cost_deviation: float | None = Field(
        default=None,
        description="代价偏差比，实际代价/预期代价，无历史目标时为 None。1.0 表示符合预期，>1 超出预期",
    )
    decision: Literal["continue", "downgrade", "switch", "new"] = Field(
        description=(
            "决策结论："
            "continue=继续推进原目标，"
            "downgrade=降低期望值继续，"
            "switch=切换到新目标，"
            "new=首次分析无历史目标"
        ),
    )
    decision_reason: str = Field(
        description="决策原因，说明为什么做出此判断",
    )
    feasibility_issues: list[str] = Field(
        default_factory=list,
        description="可行性问题列表，如 ['账户余额不足', '该活动已关闭']",
    )
    suggested_goal: str | None = Field(
        default=None,
        description="当 decision=switch 或 new 时，建议的新目标描述",
    )
    suggested_goal_type: str | None = Field(
        default=None,
        description="建议目标的分类标签，用于记忆系统统计",
    )


class BehaviorProfileMemory(BaseModel):
    """行为画像记忆（player_memory.behavior_profile 字段结构）。"""
    spend_tendency: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="消费倾向",
    )
    avg_spend_per_session: float = Field(
        default=0.0,
        description="每次会话平均消费金额",
    )
    preferred_content: list[str] = Field(
        default_factory=list,
        description="偏好内容类型列表，如 ['PVP', '收集', '社交']",
    )
    avg_session_minutes: float = Field(
        default=0.0,
        description="平均在线时长（分钟）",
    )


class GoalTypeStats(BaseModel):
    """单个目标类型的历史统计。"""
    total: int = Field(default=0, description="总追求次数")
    success: int = Field(default=0, description="成功次数")
    avg_cost: float = Field(default=0.0, description="平均实际代价")
    abandon_reasons: list[str] = Field(
        default_factory=list,
        description="放弃原因列表（保留最近 5 条）",
    )
```

- [ ] **Step 2: 写模型验证测试**

```python
# tests/unit/test_decision_models.py
"""decision_models 单元测试"""

import pytest
from src.core.agents.decision_models import (
    BehaviorProfileMemory,
    GoalEvaluationResult,
    GoalTypeStats,
    InferredIntent,
)


def test_inferred_intent_defaults():
    intent = InferredIntent(session_summary="玩家本次在线约 30 分钟")
    assert intent.completed == []
    assert intent.abandoned == []
    assert intent.next_likely == []
    assert intent.intent_confidence == "medium"


def test_goal_evaluation_result_new():
    result = GoalEvaluationResult(
        has_active_goal=False,
        decision="new",
        decision_reason="首次分析",
    )
    assert result.goal_progress is None
    assert result.cost_deviation is None
    assert result.feasibility_issues == []


def test_goal_evaluation_result_continue():
    result = GoalEvaluationResult(
        has_active_goal=True,
        goal_progress=0.6,
        cost_deviation=1.2,
        decision="continue",
        decision_reason="进度良好，代价略超预期但在可接受范围",
    )
    assert result.decision == "continue"
    assert result.suggested_goal is None


def test_goal_evaluation_result_switch():
    result = GoalEvaluationResult(
        has_active_goal=True,
        goal_progress=0.2,
        cost_deviation=2.5,
        decision="switch",
        decision_reason="代价严重超预期，玩家消费意愿低",
        suggested_goal="完成日常任务获取免费积分",
        suggested_goal_type="daily_quest",
    )
    assert result.decision == "switch"
    assert result.suggested_goal is not None


def test_behavior_profile_memory_defaults():
    profile = BehaviorProfileMemory()
    assert profile.spend_tendency == "medium"
    assert profile.avg_spend_per_session == 0.0
    assert profile.preferred_content == []
```

- [ ] **Step 3: 运行测试确认通过**

```bash
uv run pytest tests/unit/test_decision_models.py -v
```

期望：全部 PASSED

- [ ] **Step 4: Commit**

```bash
git add src/core/agents/decision_models.py tests/unit/test_decision_models.py
git commit -m "feat: 新增动态决策系统 Pydantic 数据模型"
```

---

### Task 6: 扩展 AnalysisState

**Files:**
- Modify: `src/core/agents/state.py`

新增三个字段供新节点读写。

- [ ] **Step 1: 修改 state.py**

在 `src/core/agents/state.py` 的 `AnalysisState` 末尾追加新字段：

```python
    # ── 动态决策系统字段 ──
    # intent_inference 节点写入，goal_evaluation 节点读取
    # 本次会话意图推断结果（InferredIntent.model_dump()）
    intent_result: dict

    # goal_evaluation 节点写入，action_reasoning 节点读取
    # 目标校验与决策结论（GoalEvaluationResult.model_dump()）
    goal_evaluation_result: dict

    # memory_update 节点读取，从 DB 加载的玩家记忆
    # behavior_profile + goal_history 合并字典
    player_memory: dict
```

- [ ] **Step 2: 更新编排图测试中的节点数断言**

打开 `tests/unit/test_orchestrator.py`，找到 `test_all_nodes_registered` 的期望集合，将新节点加入（此步在 Task 9 实际注册节点后才有意义，先记录此处需同步修改）。

> 注意：此步骤在 Task 9 完成后执行，此处仅作提醒。

- [ ] **Step 3: Commit**

```bash
git add src/core/agents/state.py
git commit -m "feat: AnalysisState 新增动态决策系统字段"
```

---

## Chunk 4: Prompt 模板

### Task 7: 新节点 Prompt 模板

**Files:**
- Create: `src/core/agents/decision_prompts.py`

- [ ] **Step 1: 创建 prompt 文件**

```python
# src/core/agents/decision_prompts.py
"""动态决策系统 Prompt 模板。

intent_inference、goal_evaluation 节点使用的提示词。
memory_update 节点不调用 LLM，无需 prompt。
"""

# ── 意图推断 ──

INTENT_INFERENCE_SYSTEM = """你是一个玩家行为分析师，专门从会话行为序列中推断玩家意图。

你的任务是分析玩家本次在线的行为数据，判断：
1. 玩家这次完成了什么（有明确结果的行为）
2. 玩家中途放弃了什么（开始了但未完成）
3. 下次上线玩家最可能想做什么（按可能性排序，最多3条）
4. 对本次会话做一个简短总结

推断原则：
- 基于实际行为数据，不要过度推测
- 行为序列较短或模糊时，置信度设为 low
- next_likely 要结合玩家历史记忆中的偏好和目标历史
- 如果行为序列为空，session_summary 说明数据不足，next_likely 参考历史记忆推断"""

INTENT_INFERENCE_USER = """玩家 ID：{user_id}

本次会话行为序列（按时间排序）：
{session_events}

玩家长期记忆：
{player_memory}

历史意图记录（最近3次）：
{recent_intents}

请推断玩家本次会话意图和下次最可能的行为。"""


# ── 目标校验与决策 ──

GOAL_EVALUATION_SYSTEM = """你是一个决策分析师，负责评估玩家目标的进度和代价，做出调整决策。

你的任务：
1. 如果玩家有进行中的目标，评估完成度和代价偏差，决定是继续、降级还是切换
2. 如果玩家没有历史目标，结合意图推断和玩家记忆生成新目标
3. 检查目标可行性（账户余额、游戏内条件、玩家意愿）

决策标准：
- continue（继续）：进度合理，代价偏差在 1.5 倍以内，玩家有继续意愿
- downgrade（降低期望）：进度慢但玩家有意愿，或代价略超但余额充足
- switch（切换目标）：代价严重超预期（>2倍）且玩家历史消费意愿低；或目标已明显不可行
- new（首次/无历史）：没有进行中的目标

可行性检查要点：
- 账户余额是否支撑继续（结合消费倾向判断）
- 游戏内条件是否满足（活动是否开放、道具是否存在）
- 历史消费倾向（low 倾向的玩家对代价更敏感）

feasibility_issues 只列实际存在的问题，不要虚构。"""

GOAL_EVALUATION_USER = """玩家快照：
{snapshot_text}

意图推断结果：
{intent_result}

玩家长期记忆（行为画像 + 目标历史）：
{player_memory}

上次目标记录：
{last_intent_record}

请做出目标校验和决策。"""
```

- [ ] **Step 2: Commit**

```bash
git add src/core/agents/decision_prompts.py
git commit -m "feat: 新增动态决策系统 prompt 模板"
```

---

## Chunk 5: 新节点实现

### Task 8: 实现三个新节点

**Files:**
- Create: `src/core/agents/decision_nodes.py`
- Create: `tests/unit/test_decision_nodes.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_decision_nodes.py
"""动态决策节点单元测试"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── intent_inference_node ──

@pytest.mark.asyncio
async def test_intent_inference_no_session_events():
    """无会话事件时，节点应正常返回默认意图（不抛异常）"""
    from src.core.agents.decision_nodes import intent_inference_node

    state = {
        "user_id": "user-001",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "snapshot": {"level": 10},
        "player_memory": {},
    }

    mock_llm = AsyncMock()
    mock_llm.with_structured_output.return_value = mock_llm
    from src.core.agents.decision_models import InferredIntent
    mock_llm.ainvoke = AsyncMock(return_value=InferredIntent(
        session_summary="无行为数据",
        intent_confidence="low",
    ))

    with patch("src.core.agents.decision_nodes.get_llm", return_value=mock_llm), \
         patch("src.core.agents.decision_nodes._load_session_events", return_value=[]), \
         patch("src.core.agents.decision_nodes._load_recent_intents", return_value=[]):
        result = await intent_inference_node(state)

    assert "intent_result" in result
    assert result["intent_result"]["intent_confidence"] == "low"


@pytest.mark.asyncio
async def test_intent_inference_llm_failure():
    """LLM 调用失败时，节点返回错误但不崩溃"""
    from src.core.agents.decision_nodes import intent_inference_node

    state = {
        "user_id": "user-001",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "snapshot": {},
        "player_memory": {},
    }

    with patch("src.core.agents.decision_nodes.get_llm", side_effect=Exception("LLM unavailable")), \
         patch("src.core.agents.decision_nodes._load_session_events", return_value=[]), \
         patch("src.core.agents.decision_nodes._load_recent_intents", return_value=[]):
        result = await intent_inference_node(state)

    assert "errors" in result
    assert len(result["errors"]) > 0


# ── goal_evaluation_node ──

@pytest.mark.asyncio
async def test_goal_evaluation_first_time():
    """首次分析，无历史目标，decision 应为 new"""
    from src.core.agents.decision_nodes import goal_evaluation_node
    from src.core.agents.decision_models import GoalEvaluationResult

    state = {
        "user_id": "user-001",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "snapshot": {"gold": 1000},
        "intent_result": {"session_summary": "首次登录", "next_likely": ["探索地图"]},
        "player_memory": {},
    }

    mock_llm = AsyncMock()
    mock_llm.with_structured_output.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=GoalEvaluationResult(
        has_active_goal=False,
        decision="new",
        decision_reason="首次分析，无历史目标",
        suggested_goal="探索新手村，熟悉基本操作",
        suggested_goal_type="exploration",
    ))

    with patch("src.core.agents.decision_nodes.get_llm", return_value=mock_llm), \
         patch("src.core.agents.decision_nodes._load_last_intent", return_value=None):
        result = await goal_evaluation_node(state)

    assert "goal_evaluation_result" in result
    assert result["goal_evaluation_result"]["decision"] == "new"


@pytest.mark.asyncio
async def test_goal_evaluation_failure_returns_error():
    """LLM 调用失败时返回错误"""
    from src.core.agents.decision_nodes import goal_evaluation_node

    state = {
        "user_id": "user-001",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "snapshot": {},
        "intent_result": {},
        "player_memory": {},
    }

    with patch("src.core.agents.decision_nodes.get_llm", side_effect=Exception("fail")), \
         patch("src.core.agents.decision_nodes._load_last_intent", return_value=None):
        result = await goal_evaluation_node(state)

    assert "errors" in result


# ── memory_update_node ──

@pytest.mark.asyncio
async def test_memory_update_upserts_record():
    """memory_update 节点调用 upsert，不抛异常"""
    from src.core.agents.decision_nodes import memory_update_node

    state = {
        "user_id": "user-001",
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "snapshot": {"gold": 500, "play_hours": 10.5},
        "intent_result": {"session_summary": "完成任务", "next_likely": []},
        "goal_evaluation_result": {
            "decision": "continue",
            "decision_reason": "进度良好",
            "has_active_goal": True,
            "goal_progress": 0.5,
            "cost_deviation": 1.1,
            "suggested_goal_type": "quest",
        },
        "final_output": {},
        "player_memory": {},
    }

    with patch("src.core.agents.decision_nodes._upsert_player_memory", new_callable=AsyncMock) as mock_upsert, \
         patch("src.core.agents.decision_nodes._save_player_intent", new_callable=AsyncMock):
        result = await memory_update_node(state)

    mock_upsert.assert_called_once()
    assert "errors" not in result or result.get("errors") == []
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
uv run pytest tests/unit/test_decision_nodes.py -v
```

期望：FAILED（`decision_nodes` 模块不存在）

- [ ] **Step 3: 实现 decision_nodes.py**

```python
# src/core/agents/decision_nodes.py
"""动态决策系统节点。

新增三个 LangGraph 节点：
- intent_inference_node:  意图推断（读 session_events，写 intent_result）
- goal_evaluation_node:   目标校验与决策（读 intent_result + player_memory，写 goal_evaluation_result）
- memory_update_node:     更新玩家长期记忆（读 goal_evaluation_result，写 player_memory 表）
"""

import json
import logging
from typing import Any

from langchain_core.prompts import ChatPromptTemplate

from src.core.agents.decision_models import (
    BehaviorProfileMemory,
    GoalEvaluationResult,
    GoalTypeStats,
    InferredIntent,
)
from src.core.agents.decision_prompts import (
    GOAL_EVALUATION_SYSTEM,
    GOAL_EVALUATION_USER,
    INTENT_INFERENCE_SYSTEM,
    INTENT_INFERENCE_USER,
)
from src.core.agents.state import AnalysisState
from src.core.llm.factory import get_llm

logger = logging.getLogger(__name__)

_SINGLE_CALL_TIMEOUT = 60


# ── 节点1：意图推断 ──

async def intent_inference_node(state: AnalysisState) -> dict[str, Any]:
    """推断玩家本次会话意图和下次可能的行为方向。

    读取最近一次会话的 session_events，结合历史意图记录和玩家记忆，
    用 LLM 推断本次意图并预测下次行为。
    """
    import asyncio

    user_id = state["user_id"]
    tenant_id = state["tenant_id"]
    player_memory = state.get("player_memory") or {}

    try:
        session_events = await _load_session_events(user_id, tenant_id)
        recent_intents = await _load_recent_intents(user_id, tenant_id, limit=3)

        session_events_text = (
            json.dumps(session_events, ensure_ascii=False, indent=2)
            if session_events
            else "（本次会话无行为事件数据）"
        )
        player_memory_text = (
            json.dumps(player_memory, ensure_ascii=False, indent=2)
            if player_memory
            else "（暂无玩家记忆，首次分析）"
        )
        recent_intents_text = (
            json.dumps(recent_intents, ensure_ascii=False, indent=2)
            if recent_intents
            else "（无历史意图记录）"
        )

        prompt = ChatPromptTemplate.from_messages([
            ("system", INTENT_INFERENCE_SYSTEM),
            ("human", INTENT_INFERENCE_USER),
        ])

        llm = await get_llm(model_type="fast")
        llm = llm.with_structured_output(InferredIntent, method="function_calling")
        chain = prompt | llm

        intent: InferredIntent = await asyncio.wait_for(
            chain.ainvoke({
                "user_id": user_id,
                "session_events": session_events_text,
                "player_memory": player_memory_text,
                "recent_intents": recent_intents_text,
            }),
            timeout=_SINGLE_CALL_TIMEOUT,
        )

        logger.info(
            "[intent_inference] 推断完成, user_id=%s, confidence=%s, next_count=%d",
            user_id,
            intent.intent_confidence,
            len(intent.next_likely),
        )
        return {"intent_result": intent.model_dump()}

    except Exception as e:
        logger.error("[intent_inference] 意图推断失败: %s", e)
        return {
            "intent_result": InferredIntent(
                session_summary="意图推断失败，使用空默认值",
                intent_confidence="low",
            ).model_dump(),
            "errors": [f"意图推断失败: {e}"],
        }


# ── 节点2：目标校验与决策 ──

async def goal_evaluation_node(state: AnalysisState) -> dict[str, Any]:
    """校验当前目标进度，做出继续/降级/切换决策。

    有历史目标时：对比完成度和代价偏差，结合玩家记忆决策。
    无历史目标时：基于意图推断生成新目标（decision=new）。
    """
    import asyncio

    user_id = state["user_id"]
    tenant_id = state["tenant_id"]
    snapshot = state.get("snapshot", {})
    intent_result = state.get("intent_result") or {}
    player_memory = state.get("player_memory") or {}

    try:
        last_intent = await _load_last_intent(user_id, tenant_id)

        snapshot_text = json.dumps(snapshot, ensure_ascii=False)
        intent_text = json.dumps(intent_result, ensure_ascii=False, indent=2)
        memory_text = (
            json.dumps(player_memory, ensure_ascii=False, indent=2)
            if player_memory
            else "（暂无玩家记忆）"
        )
        last_intent_text = (
            json.dumps(last_intent, ensure_ascii=False, indent=2)
            if last_intent
            else "（无历史目标，首次分析）"
        )

        prompt = ChatPromptTemplate.from_messages([
            ("system", GOAL_EVALUATION_SYSTEM),
            ("human", GOAL_EVALUATION_USER),
        ])

        llm = await get_llm(model_type="default")
        llm = llm.with_structured_output(GoalEvaluationResult, method="function_calling")
        chain = prompt | llm

        evaluation: GoalEvaluationResult = await asyncio.wait_for(
            chain.ainvoke({
                "snapshot_text": snapshot_text,
                "intent_result": intent_text,
                "player_memory": memory_text,
                "last_intent_record": last_intent_text,
            }),
            timeout=_SINGLE_CALL_TIMEOUT,
        )

        logger.info(
            "[goal_evaluation] 决策完成, user_id=%s, decision=%s, progress=%s",
            user_id,
            evaluation.decision,
            evaluation.goal_progress,
        )
        return {"goal_evaluation_result": evaluation.model_dump()}

    except Exception as e:
        logger.error("[goal_evaluation] 目标校验失败: %s", e)
        return {
            "goal_evaluation_result": GoalEvaluationResult(
                has_active_goal=False,
                decision="new",
                decision_reason=f"目标校验失败，回退到新目标模式: {e}",
            ).model_dump(),
            "errors": [f"目标校验失败: {e}"],
        }


# ── 节点3：更新玩家长期记忆 ──

async def memory_update_node(state: AnalysisState) -> dict[str, Any]:
    """更新玩家长期记忆。

    两个操作：
    1. upsert player_memory：增量更新行为画像（每次），目标历史（出现≥2次后统计）
    2. insert player_intent：记录本次意图推断和决策结论

    不调用 LLM，纯数据操作。
    """
    user_id = state["user_id"]
    tenant_id = state["tenant_id"]
    snapshot = state.get("snapshot", {})
    intent_result = state.get("intent_result") or {}
    goal_eval = state.get("goal_evaluation_result") or {}

    try:
        # 步骤1：upsert player_memory
        await _upsert_player_memory(user_id, tenant_id, snapshot, intent_result, goal_eval)

        # 步骤2：insert player_intent 记录
        await _save_player_intent(user_id, tenant_id, intent_result, goal_eval)

        logger.info("[memory_update] 完成, user_id=%s", user_id)
        return {}

    except Exception as e:
        logger.error("[memory_update] 记忆更新失败: %s", e)
        return {"errors": [f"记忆更新失败: {e}"]}


# ── 内部辅助函数 ──

async def _load_session_events(user_id: str, tenant_id: str) -> list[dict]:
    """加载最近一次会话的事件序列（按 session_id 最新的一组）。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    async with get_session() as session:
        # 先找最新的 session_id
        latest = await session.execute(
            text("""
                SELECT session_id
                FROM session_events
                WHERE user_id = :user_id AND tenant_id = :tenant_id
                ORDER BY occurred_at DESC
                LIMIT 1
            """),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        row = latest.first()
        if not row:
            return []

        session_id = row.session_id

        # 加载该 session 的所有事件（最多100条）
        events_result = await session.execute(
            text("""
                SELECT event_type, event_data, snapshot, occurred_at
                FROM session_events
                WHERE user_id = :user_id
                  AND tenant_id = :tenant_id
                  AND session_id = :session_id
                ORDER BY occurred_at ASC
                LIMIT 100
            """),
            {"user_id": user_id, "tenant_id": tenant_id, "session_id": session_id},
        )
        rows = events_result.fetchall()

    return [
        {
            "event_type": r.event_type,
            "event_data": r.event_data,
            "occurred_at": r.occurred_at.isoformat(),
        }
        for r in rows
    ]


async def _load_recent_intents(user_id: str, tenant_id: str, limit: int = 3) -> list[dict]:
    """加载最近 N 次的意图推断记录。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT inferred_intent, current_goal, goal_status,
                       goal_progress, evaluation_result, created_at
                FROM player_intent
                WHERE user_id = :user_id AND tenant_id = :tenant_id
                ORDER BY created_at DESC
                LIMIT :limit
            """),
            {"user_id": user_id, "tenant_id": tenant_id, "limit": limit},
        )
        rows = result.fetchall()

    return [
        {
            "inferred_intent": r.inferred_intent,
            "current_goal": r.current_goal,
            "goal_status": r.goal_status,
            "goal_progress": r.goal_progress,
            "evaluation_result": r.evaluation_result,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


async def _load_last_intent(user_id: str, tenant_id: str) -> dict | None:
    """加载最近一次的目标记录（仅 active 状态）。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    async with get_session() as session:
        result = await session.execute(
            text("""
                SELECT current_goal, goal_type, goal_status, goal_progress,
                       cost_expected, cost_actual, evaluation_result, created_at
                FROM player_intent
                WHERE user_id = :user_id
                  AND tenant_id = :tenant_id
                  AND goal_status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        row = result.first()

    if not row:
        return None

    return {
        "current_goal": row.current_goal,
        "goal_type": row.goal_type,
        "goal_status": row.goal_status,
        "goal_progress": row.goal_progress,
        "cost_expected": row.cost_expected,
        "cost_actual": row.cost_actual,
        "evaluation_result": row.evaluation_result,
        "created_at": row.created_at.isoformat(),
    }


async def _upsert_player_memory(
    user_id: str,
    tenant_id: str,
    snapshot: dict,
    intent_result: dict,
    goal_eval: dict,
) -> None:
    """Upsert player_memory 记录。

    行为画像每次增量更新（滑动平均）。
    目标历史在 goal_type 有值时累计统计。
    """
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    async with get_session() as session:
        # 读取现有记录
        existing = await session.execute(
            text("""
                SELECT id, behavior_profile, goal_history, analysis_count
                FROM player_memory
                WHERE user_id = :user_id AND tenant_id = :tenant_id
            """),
            {"user_id": user_id, "tenant_id": tenant_id},
        )
        row = existing.first()

        now_text = "now()"  # 使用 DB 时间

        if row is None:
            # 首次插入
            new_profile = _build_initial_behavior_profile(snapshot)
            new_goal_history: dict = {}

            await session.execute(
                text("""
                    INSERT INTO player_memory (
                        tenant_id, user_id,
                        behavior_profile, goal_history,
                        analysis_count, created_at, updated_at
                    ) VALUES (
                        :tenant_id, :user_id,
                        :behavior_profile, :goal_history,
                        1, now(), now()
                    )
                """),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "behavior_profile": json.dumps(new_profile, ensure_ascii=False),
                    "goal_history": json.dumps(new_goal_history, ensure_ascii=False),
                },
            )
        else:
            # 增量更新
            existing_profile = row.behavior_profile or {}
            existing_goal_history = row.goal_history or {}
            analysis_count = (row.analysis_count or 0) + 1

            updated_profile = _update_behavior_profile(existing_profile, snapshot, analysis_count)
            updated_goal_history = _update_goal_history(
                existing_goal_history,
                goal_eval,
                analysis_count,
            )

            await session.execute(
                text("""
                    UPDATE player_memory
                    SET behavior_profile = :behavior_profile,
                        goal_history = :goal_history,
                        analysis_count = :analysis_count,
                        updated_at = now()
                    WHERE id = :id
                """),
                {
                    "behavior_profile": json.dumps(updated_profile, ensure_ascii=False),
                    "goal_history": json.dumps(updated_goal_history, ensure_ascii=False),
                    "analysis_count": analysis_count,
                    "id": row.id,
                },
            )


def _build_initial_behavior_profile(snapshot: dict) -> dict:
    """从快照构建初始行为画像。"""
    return BehaviorProfileMemory(
        avg_spend_per_session=float(snapshot.get("gold_spent", 0) or 0),
        avg_session_minutes=float(snapshot.get("session_minutes", 0) or 0),
    ).model_dump()


def _update_behavior_profile(existing: dict, snapshot: dict, count: int) -> dict:
    """滑动平均更新行为画像。count 为更新后的累计次数。"""
    profile = BehaviorProfileMemory(**existing) if existing else BehaviorProfileMemory()

    # 滑动平均：新均值 = 旧均值 * (n-1)/n + 新值 * 1/n
    n = count
    new_spend = float(snapshot.get("gold_spent", 0) or 0)
    new_minutes = float(snapshot.get("session_minutes", 0) or 0)

    profile.avg_spend_per_session = (
        profile.avg_spend_per_session * (n - 1) / n + new_spend / n
        if n > 0 else new_spend
    )
    profile.avg_session_minutes = (
        profile.avg_session_minutes * (n - 1) / n + new_minutes / n
        if n > 0 else new_minutes
    )

    # 消费倾向判断（简单规则）
    avg = profile.avg_spend_per_session
    if avg > 500:
        profile.spend_tendency = "high"
    elif avg > 100:
        profile.spend_tendency = "medium"
    else:
        profile.spend_tendency = "low"

    return profile.model_dump()


def _update_goal_history(existing: dict, goal_eval: dict, analysis_count: int) -> dict:
    """累计更新目标历史统计。同一 goal_type 出现 >=2 次后才写入。"""
    goal_type = goal_eval.get("suggested_goal_type") or goal_eval.get("goal_type")
    if not goal_type:
        return existing

    history = dict(existing)
    entry = history.get(goal_type, {"total": 0, "success": 0, "avg_cost": 0.0, "abandon_reasons": []})

    entry["total"] = entry.get("total", 0) + 1

    decision = goal_eval.get("decision")
    if decision == "continue":
        # 视为本轮成功推进
        pass
    elif decision in ("switch", "downgrade"):
        reason = goal_eval.get("decision_reason", "")
        reasons = entry.get("abandon_reasons", [])
        reasons.append(reason)
        entry["abandon_reasons"] = reasons[-5:]  # 保留最近5条

    # goal_progress >= 1.0 视为完成
    progress = goal_eval.get("goal_progress") or 0.0
    if progress >= 1.0:
        entry["success"] = entry.get("success", 0) + 1

    # 代价统计（滑动平均）
    cost_deviation = goal_eval.get("cost_deviation")
    if cost_deviation is not None:
        n = entry["total"]
        entry["avg_cost"] = (
            entry.get("avg_cost", 0.0) * (n - 1) / n + cost_deviation / n
            if n > 0 else cost_deviation
        )

    # 出现 >=2 次才写入 history（避免偶发行为污染）
    if entry["total"] >= 2:
        history[goal_type] = entry

    return history


async def _save_player_intent(
    user_id: str,
    tenant_id: str,
    intent_result: dict,
    goal_eval: dict,
) -> None:
    """写入本次意图推断和决策结论到 player_intent 表。"""
    from sqlalchemy import text

    from src.core.infrastructure.db import get_session

    current_goal = goal_eval.get("suggested_goal") or intent_result.get("session_summary", "")
    goal_type = goal_eval.get("suggested_goal_type")
    decision = goal_eval.get("decision", "new")
    decision_reason = goal_eval.get("decision_reason", "")
    goal_progress = goal_eval.get("goal_progress")
    cost_deviation = goal_eval.get("cost_deviation")

    # 将 decision 映射为 goal_status
    goal_status_map = {
        "continue": "active",
        "downgrade": "active",
        "switch": "switched",
        "new": "active",
    }
    goal_status = goal_status_map.get(decision, "active")

    async with get_session() as session:
        await session.execute(
            text("""
                INSERT INTO player_intent (
                    tenant_id, user_id,
                    inferred_intent, current_goal, goal_type,
                    goal_status, goal_progress,
                    cost_actual, evaluation_result, evaluation_reason
                ) VALUES (
                    :tenant_id, :user_id,
                    :inferred_intent, :current_goal, :goal_type,
                    :goal_status, :goal_progress,
                    :cost_actual, :evaluation_result, :evaluation_reason
                )
            """),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "inferred_intent": json.dumps(intent_result, ensure_ascii=False),
                "current_goal": current_goal,
                "goal_type": goal_type,
                "goal_status": goal_status,
                "goal_progress": goal_progress,
                "cost_actual": cost_deviation,
                "evaluation_result": decision,
                "evaluation_reason": decision_reason,
            },
        )
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/unit/test_decision_nodes.py -v
```

期望：全部 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/core/agents/decision_nodes.py tests/unit/test_decision_nodes.py
git commit -m "feat: 实现 intent_inference、goal_evaluation、memory_update 节点"
```

---

## Chunk 6: 注册节点到编排图

### Task 9: 更新 Orchestrator

**Files:**
- Modify: `src/core/agents/orchestrator.py`
- Modify: `tests/unit/test_orchestrator.py`

将三个新节点插入图中。新流程：

```
START → fetch_snapshot → retrieve_rag_context
      → intent_inference（新，读 session_events + player_memory）
      → goal_evaluation（新，读 intent_result + player_memory）
      → gather_context
      → behavior_analysis
      → action_reasoning
      → merge_output
      → tracking_update
      → memory_update（新，写 player_memory 表）
      → END
```

`intent_inference` 和 `goal_evaluation` 在 `gather_context` 之前运行，
确保 `goal_evaluation_result` 可以注入到 `action_reasoning` 的上下文中。

- [ ] **Step 1: 更新测试期望**

打开 `tests/unit/test_orchestrator.py`，找到节点断言，更新为 10 个节点：

```python
# test_all_nodes_registered 中
expected_nodes = {
    "fetch_snapshot",
    "retrieve_rag_context",
    "intent_inference",       # 新增
    "goal_evaluation",        # 新增
    "gather_context",
    "behavior_analysis",
    "action_reasoning",
    "merge_output",
    "tracking_update",
    "memory_update",          # 新增
}
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
uv run pytest tests/unit/test_orchestrator.py -v
```

期望：FAILED（节点数不匹配）

- [ ] **Step 3: 更新 orchestrator.py**

```python
# src/core/agents/orchestrator.py
"""主协调图 — Orchestrator。

图结构:
START → fetch_snapshot → retrieve_rag_context
      → intent_inference → goal_evaluation
      → gather_context
      → behavior_analysis → action_reasoning → merge_output
      → tracking_update → memory_update → END

Checkpointer: PostgresSaver，状态持久化到 PostgreSQL。
"""

import logging

from langgraph.graph import END, START, StateGraph

from src.core.agents.nodes import (
    action_reasoning_node,
    behavior_analysis_node,
    fetch_snapshot_node,
    gather_context_node,
    merge_output_node,
    retrieve_rag_context_node,
    tracking_update_node,
)
from src.core.agents.decision_nodes import (
    goal_evaluation_node,
    intent_inference_node,
    memory_update_node,
)
from src.core.agents.state import AnalysisState

logger = logging.getLogger(__name__)


def build_orchestrator() -> StateGraph:
    """构建主协调图（不含 checkpointer）"""
    builder = StateGraph(AnalysisState)

    # 注册节点
    builder.add_node("fetch_snapshot", fetch_snapshot_node)
    builder.add_node("retrieve_rag_context", retrieve_rag_context_node)
    builder.add_node("intent_inference", intent_inference_node)
    builder.add_node("goal_evaluation", goal_evaluation_node)
    builder.add_node("gather_context", gather_context_node)
    builder.add_node("behavior_analysis", behavior_analysis_node)
    builder.add_node("action_reasoning", action_reasoning_node)
    builder.add_node("merge_output", merge_output_node)
    builder.add_node("tracking_update", tracking_update_node)
    builder.add_node("memory_update", memory_update_node)

    # 线性边
    builder.add_edge(START, "fetch_snapshot")
    builder.add_edge("fetch_snapshot", "retrieve_rag_context")
    builder.add_edge("retrieve_rag_context", "intent_inference")
    builder.add_edge("intent_inference", "goal_evaluation")
    builder.add_edge("goal_evaluation", "gather_context")
    builder.add_edge("gather_context", "behavior_analysis")
    builder.add_edge("behavior_analysis", "action_reasoning")
    builder.add_edge("action_reasoning", "merge_output")
    builder.add_edge("merge_output", "tracking_update")
    builder.add_edge("tracking_update", "memory_update")
    builder.add_edge("memory_update", END)

    return builder


async def create_orchestrator():
    """创建带 PostgresSaver checkpointer 的编译图。"""
    builder = build_orchestrator()

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from src.config import settings

    checkpointer = AsyncPostgresSaver.from_conn_string(str(settings.postgres_dsn))
    await checkpointer.setup()

    graph = builder.compile(checkpointer=checkpointer)
    logger.info("[orchestrator] 主图编译完成, nodes=10, checkpointer=PostgresSaver")
    return graph
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run pytest tests/unit/test_orchestrator.py -v
```

期望：全部 PASSED

- [ ] **Step 5: 运行全量单元测试，确认无回归**

```bash
uv run pytest tests/unit/ -v
```

期望：全部 PASSED

- [ ] **Step 6: Commit**

```bash
git add src/core/agents/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat: 将 intent_inference、goal_evaluation、memory_update 注册到编排图"
```

---

## Chunk 7: action_reasoning 上下文扩展

### Task 10: 将决策结论注入行动推理

**Files:**
- Modify: `src/core/agents/prompts.py`
- Modify: `src/core/agents/nodes.py`

`action_reasoning_node` 已有 `tracking_summary` 和 `anomaly_text` 上下文，
再注入 `goal_evaluation_result` 和 `intent_result`，让推荐行动与决策结论保持一致。

- [ ] **Step 1: 更新 ACTION_REASONING_USER prompt**

在 `src/core/agents/prompts.py` 的 `ACTION_REASONING_USER` 末尾追加两个新占位符：

```python
ACTION_REASONING_USER = """行为分析报告：
{behavior_report}

实体快照：
{snapshot_text}

领域规则上下文：
{rag_context}

历史趋势与额外上下文：
{enriched_context}

上次推荐行动完成情况：
{tracking_summary}

当前异常检测结果：
{anomaly_text}

意图推断结果（玩家本次想做什么 / 下次最可能做什么）：
{intent_result}

目标校验决策（continue / downgrade / switch / new）：
{goal_evaluation_result}"""
```

同时在 `ACTION_REASONING_SYSTEM` 末尾追加决策对齐要求：

```python
# 在 ACTION_REASONING_SYSTEM 末尾添加：
"""
决策对齐要求：
- 如果 goal_evaluation_result.decision=continue，推荐行动应与现有目标方向一致，帮助玩家继续推进
- 如果 decision=downgrade，推荐更容易达成的子目标，降低难度和代价
- 如果 decision=switch，推荐与 suggested_goal 对齐的新方向，不再推进原目标
- 如果 decision=new，结合 intent_result.next_likely 和玩家历史记忆推荐起始目标
- intent_result.next_likely 中排名第一的意图应优先体现在推荐行动中"""
```

- [ ] **Step 2: 更新 action_reasoning_node 注入新上下文**

在 `src/core/agents/nodes.py` 的 `action_reasoning_node` 中，读取并注入新字段：

```python
# 在 action_reasoning_node 内，读取现有字段之后添加：
intent_result = state.get("intent_result") or {}
goal_evaluation_result = state.get("goal_evaluation_result") or {}
intent_text = json.dumps(intent_result, ensure_ascii=False) if intent_result else "（无意图推断数据）"
goal_eval_text = json.dumps(goal_evaluation_result, ensure_ascii=False) if goal_evaluation_result else "（无目标校验数据）"
```

并在 `chain.ainvoke` 调用中加入两个新参数：

```python
action_list: ActionList | None = await asyncio.wait_for(
    chain.ainvoke({
        "behavior_report": behavior_report,
        "snapshot_text": snapshot_text,
        "rag_context": rag_context,
        "enriched_context": enriched_context,
        "tracking_summary": tracking_summary,
        "anomaly_text": anomaly_text,
        "intent_result": intent_text,                    # 新增
        "goal_evaluation_result": goal_eval_text,        # 新增
    }),
    timeout=_SINGLE_CALL_TIMEOUT,
)
```

- [ ] **Step 3: 运行全量单元测试**

```bash
uv run pytest tests/unit/ -v
```

期望：全部 PASSED

- [ ] **Step 4: 运行全量 API 测试**

```bash
uv run pytest tests/api/ -v
```

期望：全部 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/core/agents/prompts.py src/core/agents/nodes.py
git commit -m "feat: action_reasoning 注入 intent_result 和 goal_evaluation_result 上下文"
```

---

## Chunk 8: 收尾验证

### Task 11: 完整测试套件验证

- [ ] **Step 1: 运行所有单元和 API 测试**

```bash
uv run pytest tests/unit/ tests/api/ -v
```

期望：全部 PASSED，无回归

- [ ] **Step 2: 验证迁移状态**

```bash
uv run alembic current
```

期望：`007 (head)`

- [ ] **Step 3: 最终 Commit**

```bash
git add .
git commit -m "feat: 动态决策系统完整实现（session_events + player_intent + player_memory + 3节点 + behavior webhook）"
```

---

## 实现顺序总结

| Chunk | 内容 | 依赖 |
|-------|------|------|
| 1 | 数据库迁移（3张表） | 无 |
| 2 | Webhook behavior 端点 | Chunk 1（session_events 表） |
| 3 | Pydantic 模型 + State 扩展 | 无 |
| 4 | Prompt 模板 | Chunk 3 |
| 5 | 三个新节点实现 | Chunk 3、4 |
| 6 | 注册节点到编排图 | Chunk 5 |
| 7 | action_reasoning 上下文扩展 | Chunk 6 |
| 8 | 收尾验证 | 全部 |
