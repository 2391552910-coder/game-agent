# 可执行动作输出改造计划

**目标**：让 myAgent2 从输出高层策略（"建议去健身房"）升级为输出具体可执行动作序列（坐标、路径点、动作名），供 HostingGateway 直接发协议。

---

## 任务一：把地图坐标和动作枚举导入 LightRAG

### 背景

当前 LightRAG 知识库只有领域规则文档。大模型检索时拿不到"健身房坐标是(120,0,85)"这类具体数据，因此无法输出精确的执行参数。

### 需要做什么

**1. 准备知识文档**

把地图坐标和动作枚举整理成自然语言文本，再用 `rag.ainsert()` 导入。格式示例：

```
# 地点坐标表
商业区入口：坐标 (120, 0, 85)
健身房大门：坐标 (134, 0, 92)
咖啡厅：坐标 (98, 0, 110)
广场中心：坐标 (200, 0, 200)
```

```
# 可用动作枚举
move_to：移动到指定坐标，参数 target(x,y,z)，speed(walk|run)
jump：跳跃，无参数
play_action：执行动作，参数 name，可选值：wave（挥手）、sit（坐下）、dance（跳舞）、greet（打招呼）
stop_move：停止移动，无参数
enter_building：进入建筑，参数 building_id
```

**2. 编写导入脚本**

在 `scripts/ingest_knowledge.py`（新建）中调用 `rag.ainsert()`，从文件读取地图和动作文档并导入：

```python
import asyncio
from pathlib import Path
from src.core.engine.lightrag_engine import get_rag, shutdown_rag

async def main():
    rag = await get_rag()
    docs_dir = Path("docs/knowledge")
    for f in docs_dir.glob("*.txt"):
        text = f.read_text(encoding="utf-8")
        await rag.ainsert(text)
        print(f"已导入: {f.name}")
    await shutdown_rag()

asyncio.run(main())
```

**3. 知识文档存放位置**

新建目录 `docs/knowledge/`，存放：
- `map_locations.txt`：地点名称 + 坐标
- `action_enum.txt`：动作名称 + 参数说明
- `path_hints.txt`（可选）：常用路径提示，如"从广场到健身房需途经商业区入口"

**4. 验证**

导入后在 Neo4j Browser 查询是否出现地点和动作节点：
```cypher
MATCH (n) WHERE n.entity_id CONTAINS '健身房' OR n.entity_id CONTAINS 'move_to' RETURN n
```

---

## 任务二：修改 RecommendedAction 模型

### 背景

当前 `payload: dict` 是无结构的空字典，LLM 不知道该往里填什么，输出随意。需要把执行层所需的字段明确定义出来。

### 需要修改的文件

`src/core/agents/models.py`

### 改动方案

在 `RecommendedAction` 中把 `payload` 替换为结构化的执行参数字段，新增 `ActionStep` 和 `Waypoint` 子模型：

```python
from pydantic import BaseModel, Field
from typing import Literal

class Waypoint(BaseModel):
    """路径点坐标"""
    x: float
    y: float
    z: float
    label: str | None = Field(default=None, description="地点名称，如'健身房大门'")

class ActionStep(BaseModel):
    """单步可执行动作"""
    skill: Literal["move_to", "stop_move", "jump", "play_action", "enter_building"] = Field(
        description="技能名称，必须是 HostingGateway 支持的白名单 skill"
    )
    params: dict = Field(
        default_factory=dict,
        description=(
            "执行参数。"
            "move_to: {target: {x,y,z}, speed: walk|run, label: '地点名'}；"
            "play_action: {name: '动作名'}；"
            "enter_building: {building_id: '建筑ID'}；"
            "jump/stop_move: {}"
        ),
    )
    reason: str | None = Field(default=None, description="这一步的原因")

class RecommendedAction(BaseModel):
    """推荐行动。

    action_steps 是可直接发给 HostingGateway 的动作序列。
    goal_metric / goal_value / expected_hours 用于后续追踪。
    """
    action_type: str = Field(description="行为类型，如'前往健身房'、'执行打招呼动作'")
    priority: Literal["high", "medium", "low"]
    reason: str = Field(description="推荐原因")

    # ── 可执行动作序列（新增）──
    action_steps: list[ActionStep] = Field(
        default_factory=list,
        description=(
            "按顺序执行的动作步骤列表。"
            "LLM 从 RAG 上下文中读取坐标和动作枚举后填写。"
            "如果知识库中找不到坐标，steps 可为空，由 HostingGateway 自行路径规划。"
        ),
    )
    waypoints: list[Waypoint] = Field(
        default_factory=list,
        description="路径规划途经点，按顺序排列。move_to 类行动使用。",
    )

    # ── 监督机制追踪字段（保持不变）──
    goal_metric: str | None = Field(default=None, description="完成判断指标，对应快照数值字段名")
    goal_value: float | None = Field(default=None, description="目标值")
    expected_hours: int | None = Field(default=None, description="预计完成小时数")
```

---

## 任务三：修改 action_reasoning 的 prompt

### 背景

当前 `ACTION_REASONING_SYSTEM` 要求 LLM 输出"可执行、有具体目标"，但没有要求填坐标和动作步骤，LLM 自然不会填。需要在 prompt 中明确告知 LLM 坐标从 RAG 上下文中读取，并要求输出 `action_steps`。

### 需要修改的文件

`src/core/agents/prompts.py`

### 改动方案

在 `ACTION_REASONING_SYSTEM` 末尾追加"执行参数填写要求"段落：

```
执行参数填写要求：
- 每个行动必须尽量填写 action_steps，列出按顺序执行的具体动作
- 地点坐标从 rag_context 或 enriched_context 中读取，格式为 {x, y, z}
- skill 名称必须是白名单之一：move_to、stop_move、jump、play_action、enter_building
- move_to 的 params 必须包含 target（坐标对象）和 speed（walk 或 run）
- play_action 的 params 必须包含 name（动作枚举值）
- 如果 rag_context 中找不到目标地点的坐标，action_steps 留空，在 reason 中说明缺少坐标
- waypoints 用于多段路径，按途经顺序填写
```

在 `ACTION_REASONING_USER` 末尾追加一段提示，让 LLM 注意从上下文中提取坐标：

```
注意：请从上方"领域规则上下文"和"历史趋势与额外上下文"中提取地点坐标和可用动作枚举，
填写每个行动的 action_steps 和 waypoints。
```

---

## 任务四：对接 HostingGateway

### 背景

myAgent2 只负责决策，不发协议。`final_output` 中的 `recommended_actions` 需要被 HostingGateway 消费。

### 当前输出格式

myAgent2 通过 `/api/analysis` 接口（或 Prefect Flow 回调）返回：

```json
{
  "player_profile": { ... },
  "recommended_actions": [
    {
      "action_type": "前往健身房",
      "priority": "high",
      "reason": "...",
      "action_steps": [
        {"skill": "move_to", "params": {"target": {"x": 120, "y": 0, "z": 85}, "speed": "run", "label": "商业区入口"}},
        {"skill": "move_to", "params": {"target": {"x": 134, "y": 0, "z": 92}, "speed": "walk", "label": "健身房大门"}},
        {"skill": "enter_building", "params": {"building_id": "gym_01"}}
      ],
      "waypoints": [
        {"x": 120, "y": 0, "z": 85, "label": "商业区入口"},
        {"x": 134, "y": 0, "z": 92, "label": "健身房大门"}
      ]
    }
  ]
}
```

### 对接方式

HostingGateway 从以下任一途径获取 myAgent2 的输出：

**方式A（推荐）：回调 Webhook**

myAgent2 分析完成后，主动 POST 到 HostingGateway 的回调地址：

```
POST http://hosting-gateway/api/decision_callback
Body: { "user_id": "...", "tenant_id": "...", "actions": [...] }
```

在 `src/core/flows/analysis_flow.py`（Prefect Flow）中，图执行完成后追加回调逻辑。

**方式B：HostingGateway 主动拉取**

HostingGateway 在合适时机调用 myAgent2 的查询接口：

```
GET /api/analysis/latest?user_id=xxx&tenant_id=xxx
```

需要在 `src/api/routes/analysis.py` 新增该接口，从 `analysis_results` 表读取最新一条记录返回。

**边界约定**

- myAgent2 不发任何游戏协议
- myAgent2 不维护游戏连接、心跳、会话状态
- `action_steps` 中坐标由 myAgent2 通过 LightRAG 检索填写，但 HostingGateway 可以选择忽略并自行路径规划
- `action_steps` 中 skill 名称必须与 HostingGateway 白名单对齐，双方需提前约定枚举值

---

## 执行顺序建议

1. 先完成**任务一**（导入知识文档），这是后续 LLM 能输出坐标的前提
2. 再完成**任务二**（修改模型），定义输出结构
3. 再完成**任务三**（修改 prompt），让 LLM 知道要填什么
4. 最后完成**任务四**（对接方式），与 HostingGateway 约定接口格式

任务一和任务四可以并行推进，互不依赖。
