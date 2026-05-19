# RobotGateway 对接指南

面向游戏开发团队。说明如何将 RobotGateway 对接到 myAgent2 平台，使平台能自动分析玩家行为并生成推荐。

---

## 对接概览

对接只需要做 **三件事**：

```
1. 注册租户，获取 API Key
2. 发送玩家在线/离线事件 (Webhook)
3. 接收分析结果回调 (HTTP Callback)
```

平台自动完成其余工作：获取玩家数据、检索游戏规则、AI 分析、存储结果，并主动推送分析结果。

---

## 第一步：注册租户

调用注册接口，获取唯一 API Key。

```bash
curl -X POST http://localhost:8000/api/v1/tenants/register \
  -H "Content-Type: application/json" \
  -d '{"user_id": "my_game_alpha"}'
```

响应：

```json
{
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "api_key": "gap_a1b2c3d4e5f6789012345678",
  "user_id": "my_game_alpha"
}
```

**保存 `api_key`**，后续所有 API 调用都需要通过 `X-API-Key` Header 传递。

> **注意**: `user_id` 全局唯一，重复注册会返回 409 错误。

---

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

---

## 第二步：发送玩家事件

RobotGateway 在检测到玩家上线或离线时，向平台发送事件通知。

### 离线事件（触发分析）

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

响应：

```json
{"status": "scheduled", "user_id": "player_12345", "flow_run_id": "flow-a1b2c3d4e5f67890"}
```

| status | 含义 |
|--------|------|
| `scheduled` | 首次离线，已调度分析任务 |
| `debounced` | 近期内已有分析在执行或等待，本次忽略 |

### 上线事件（取消分析）

```bash
curl -X POST http://localhost:8000/webhooks/player-event \
  -H "Content-Type: application/json" \
  -H "X-API-Key: gap_a1b2c3d4e5f6789012345678" \
  -d '{
    "user_id": "player_12345",
    "event_type": "online",
    "timestamp": 1744100300.5
  }'
```

响应：

```json
{"status": "cancelled", "user_id": "player_12345"}
```

如果玩家在分析完成前重新上线（短暂离线又回来），平台会取消未完成的分析，避免浪费资源。

### 事件字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_id` | string | 是 | 玩家在游戏内的唯一标识 |
| `event_type` | string | 是 | `"online"` 或 `"offline"` |
| `timestamp` | float | 是 | 事件发生时间（Unix 时间戳，秒） |
| `snapshot` | object | 否 | 玩家快照数据（见下文） |

---

## 第三步：接收分析结果回调

分析在后台异步执行（通常 10-30 秒）。分析完成后，myAgent2 会主动向配置的 `ROBOTGATEWAY_CALLBACK_URL` 发送 HTTP POST 请求。

### 回调请求格式

**URL**: 由 `ROBOTGATEWAY_CALLBACK_URL` 环境变量配置

**请求头**:
- `Content-Type: application/json`
- `X-Callback-API-Key: <api-key>`（仅当配置了 `ROBOTGATEWAY_CALLBACK_API_KEY` 时）

**请求体**:

```json
{
  "event_type": "analysis.completed",
  "tenant_id": "tenant_001",
  "user_id": "player_12345",
  "timestamp": "2026-04-13T15:30:00+00:00",
  "snapshot": {
    "level": 85,
    "pvp_rating": 2400
  },
  "analysis": {
    "player_profile": {
      "playstyle": "competitive",
      "engagement_level": "high",
      "current_goal": ["提升PVP段位"],
      "bottlenecks": ["装备评分不足"]
    },
    "recommended_actions": [
      {
        "action_type": "pvp_arena",
        "priority": "high",
        "reason": "玩家PVP评分1800，推荐参与竞技场冲击段位奖励",
        "payload": {"arena_type": "ranked"}
      }
    ]
  }
}
```

### 响应要求

RobotGateway 应返回 HTTP 204 No Content 或 200 OK，表示已成功接收回调。

---

## 内部查询接口（调试/管理用）

以下接口仅供内部查询、调试或后台管理使用，**不应作为 RobotGateway 获取分析结果的主链路**。

### 查询最新结果

```bash
curl http://localhost:8000/api/v1/analysis/player_12345/latest \
  -H "X-API-Key: gap_a1b2c3d4e5f6789012345678"
```

响应：

```json
{
  "user_id": "player_12345",
  "analyzed_at": "2026-04-13T15:30:00+00:00",
  "output": {
    "player_profile": {
      "playstyle": "competitive",
      "engagement_level": "high",
      "current_goal": ["提升PVP段位"],
      "bottlenecks": ["装备评分不足"]
    },
    "recommended_actions": [
      {
        "action_type": "pvp_arena",
        "priority": "high",
        "reason": "玩家PVP评分1800，推荐参与竞技场冲击段位奖励",
        "payload": {"arena_type": "ranked"}
      }
    ]
  }
}
```

如果分析尚未完成或无历史记录，返回 404。

### 查询历史结果

```bash
curl "http://localhost:8000/api/v1/analysis/player_12345/history?limit=5" \
  -H "X-API-Key: gap_a1b2c3d4e5f6789012345678"
```

---

## 玩家快照数据

平台需要玩家的当前状态数据来进行分析。有两种提供方式：

### 方式一：Webhook 携带（推荐用于轻量数据）

在离线事件中直接携带 `snapshot` 字段：

```json
{
  "user_id": "player_12345",
  "event_type": "offline",
  "timestamp": 1744100000.0,
  "snapshot": {
    "user_id": "player_12345",
    "player_name": "阿尔萨斯",
    "level": 85,
    "pvp_rating": 2400
  }
}
```

### 方式二：平台主动拉取（适用于大数据量或实时数据）

如果快照数据较大或需要实时查询游戏数据库，可以实现 `fetch_player_snapshot` 接口。这需要在平台侧部署游戏数据库连接器。详见 `src/game_specific/connector.py`。

---

## 快照字段参考

所有字段均为**可选**，平台能处理缺失字段。但字段越完整，分析质量越高。

### 核心字段（强烈建议提供）

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `user_id` | string | 玩家唯一 ID | `"player_12345"` |
| `player_name` | string | 玩家昵称 | `"阿尔萨斯"` |
| `level` | int | 等级/段位 | `85` |

### 资源状态

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `currencies` | dict | 货币数量 | `{"gold": 5000000, "diamond": 3200}` |
| `stamina` | int | 体力/能量 | `120` |
| `exp` | int | 当前经验 | `8560000` |

### 行为统计

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `play_hours` | float | 累计游戏时长(小时) | `1250.5` |
| `login_days` | int | 累计登录天数 | `890` |
| `online_today_hours` | float | 今日在线时长 | `3.5` |
| `last_login_at` | float | 最近登录时间戳 | `1744100000.0` |

### PVP/PVE 统计

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `pvp_rating` | int | PVP 评分/段位 | `2400` |
| `pvp_win_count` | int | PVP 胜场 | `1256` |
| `pvp_lose_count` | int | PVP 败场 | `890` |
| `pve_difficulty` | string | 常用 PVE 难度 | `"mythic"` |
| `boss_kill_count` | int | 击败 Boss 数 | `156` |
| `dungeon_clear_count` | int | 通关副本数 | `890` |

### 进度

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `main_quest_id` | string | 当前主线任务 ID | `"chapter_15"` |
| `main_quest_progress` | int | 主线进度(0-100) | `75` |
| `daily_quest_remaining` | int | 剩余日常任务 | `3` |

### 社交

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `friend_count` | int | 好友数 | `128` |
| `guild_name` | string | 公会名 | `"银色黎明"` |
| `chat_message_count` | int | 今日聊天数 | `45` |

### 自定义字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `game_specific` | dict | 游戏特有数据，平台会透传给 LLM |

`game_specific` 是万能扩展点，可以放任何游戏特有的数据。平台不会解析这个字段的内部结构，仅作为 LLM 的上下文使用。

---

## 快照最佳实践

### 字段命名

- 使用 **snake_case**（如 `play_hours` 而非 `playHours`）
- 嵌套层级**不超过 2 层**（LLM 解析深层嵌套效果差）
- 只返回**当前状态**，不返回历史记录
- 字段值使用 Python 原生类型（str, int, float, list, dict）

### 影响分析质量的关键字段

按影响程度排序：

1. **pvp_rating + pvp_win_count + pvp_lose_count** — 决定竞技类推荐的精度
2. **play_hours + login_days + online_today_hours** — 决定活跃度判断
3. **level + main_quest_progress** — 决定进度类推荐
4. **currencies + stamina** — 决定资源类推荐
5. **game_specific** — 提供游戏特有上下文

### 不同游戏类型的快照示例

#### MMO/RPG 游戏

```json
{
  "user_id": "player_12345",
  "player_name": "阿尔萨斯",
  "level": 85,
  "vip_level": 12,
  "guild_name": "银色黎明",
  "currencies": {"gold": 5000000, "diamond": 3200, "honor": 85000},
  "stamina": 120,
  "play_hours": 1250.5,
  "pvp_rating": 2400,
  "pvp_win_count": 1256,
  "pvp_lose_count": 890,
  "pve_difficulty": "mythic",
  "main_quest_id": "chapter_15",
  "main_quest_progress": 75,
  "game_specific": {
    "current_area": "冰冠堡垒",
    "profession": "死亡骑士",
    "specialization": "邪恶"
  }
}
```

#### SLG/策略游戏

```json
{
  "user_id": "player_5678",
  "player_name": "曹操",
  "level": 45,
  "currencies": {"gold": 10000000, "food": 5000000, "wood": 3000000},
  "play_hours": 680.0,
  "pvp_rating": 1850,
  "guild_name": "魏国联盟",
  "game_specific": {
    "city_level": 25,
    "march_queue_count": 3,
    "hero_stars": {"曹操": 5, "司马懿": 4}
  }
}
```

#### FPS/竞技游戏

```json
{
  "user_id": "player_9012",
  "player_name": "ShadowSniper",
  "level": 78,
  "play_hours": 890.5,
  "pvp_rating": 2850,
  "pvp_win_count": 1234,
  "pvp_lose_count": 1098,
  "game_specific": {
    "accuracy": 42.5,
    "headshot_rate": 38.2,
    "kd_ratio": 1.85,
    "favorite_weapon": "AWP",
    "rank_name": "Diamond II"
  }
}
```

---

## 知识库（游戏文档）

### 为什么需要知识库

没有游戏文档时，LLM 只能基于快照数据做泛泛的分析（如"PVP 评分高，推荐打竞技场"）。

灌入游戏文档后，LLM 的推荐会基于真实的游戏规则（如"PVP 评分 1800-2000 区间，推荐参与周末双倍积分活动，可在 2 周内晋升到黄金段位"）。

### 如何灌入

联系平台管理员，将游戏文档（Markdown、纯文本均可）提供后，由管理员导入 LightRAG 知识库。

适合灌入的文档：
- 游戏规则手册
- 活动日历与规则
- 装备/道具/技能数据表
- 段位/赛季机制说明
- 常见问题解答（FAQ）

不适合灌入的文档：
- 图片、视频
- 二进制格式（PDF 需先转文本）
- 过于频繁更新的数据（如实时排行榜）

---

## 常见问题

### 分析结果什么时候可用？

从玩家离线事件到达，到分析结果写入数据库，通常 10-30 秒。建议在离线事件发送后等待 30 秒再查询结果。

### 重复的离线事件会怎样？

平台通过 Redis 去重。同一玩家在 TTL 时间内（默认 5 分钟）多次离线事件，只会执行一次分析。后续事件返回 `{"status": "debounced"}`。

### 分析失败了怎么办？

后台分析失败时，错误会记录到平台日志，不会影响其他玩家。游戏服务器可以稍后重试查询，或在下次玩家离线时触发新的分析。

### API 调用频率有限制吗？

默认限制每个租户 100 次/分钟。如果需要更高的调用频率，请联系平台管理员。

### 快照数据安全吗？

快照数据通过 HTTPS 传输，API Key 认证，租户隔离存储。不同游戏的数据完全隔离，无法互相访问。

### 返回 401 怎么办？

检查：
1. 是否设置了 `X-API-Key` Header
2. API Key 是否正确（以 `gap_` 开头）
3. 租户是否被禁用（联系管理员）
