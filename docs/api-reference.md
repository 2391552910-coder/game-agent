# API 接口文档

myAgent 平台所有 HTTP 端点的完整参考。

- 基础 URL: `http://localhost:8000`
- 内容类型: `application/json`
- 交互式文档: `http://localhost:8000/docs` (Swagger UI)

---

## 认证

除健康检查和文档路径外，所有端点都需要认证。

**方式**: HTTP Header `X-API-Key`

```
X-API-Key: gap_a1b2c3d4e5f6789012345678
```

认证流程：
1. 从 `X-API-Key` Header 提取密钥
2. 查询 Redis 缓存（TTL 5 分钟）
3. 缓存未命中时查询 PostgreSQL `tenants` 表
4. 验证通过后设置 `request.state.tenant_id`

**公开路径**（不需要认证）:
- `GET /health`
- `GET /docs`, `GET /openapi.json`, `GET /redoc`

### 错误响应

| 状态码 | 场景 |
|--------|------|
| 401 | 缺少 `X-API-Key` Header |
| 401 | API Key 无效或租户已禁用 |
| 429 | 请求频率超过限制 |

---

## 速率限制

- 限制: **100 次/分钟**
- 维度: 租户 ID + 客户端 IP
- 算法: Redis ZSET 滑动窗口
- 超限响应: `429 {"detail": "请求过于频繁，请稍后再试"}`

---

## 端点列表

| 方法 | 路径 | 认证 | 权限 | 说明 |
|------|------|------|------|------|
| POST | `/webhooks/player-event` | API Key | 普通租户 | 接收玩家事件 |
| GET | `/api/v1/analysis/{user_id}/latest` | API Key | 普通租户 | 查询最新分析 |
| GET | `/api/v1/analysis/{user_id}/history` | API Key | 普通租户 | 查询分析历史 |
| POST | `/api/v1/tenants/register` | 无 | 公开 | 注册租户 |
| GET | `/api/v1/quota/usage` | API Key | 普通租户 | 查询配额 |
| GET | `/api/v1/providers` | API Key | 管理员 | 列出 LLM 提供商 |
| POST | `/api/v1/providers` | API Key | 管理员 | 添加 LLM 提供商 |
| PUT | `/api/v1/providers/{id}` | API Key | 管理员 | 更新 LLM 提供商 |
| DELETE | `/api/v1/providers/{id}` | API Key | 管理员 | 删除 LLM 提供商 |
| GET | `/health` | 无 | 公开 | 健康检查 |

---

## Webhook

### POST /webhooks/player-event

接收玩家在线/离线事件。

**请求体**:

```json
{
  "user_id": "player_12345",
  "event_type": "offline",
  "timestamp": 1744100000.0,
  "snapshot": null
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_id` | string | 是 | 玩家 ID |
| `event_type` | string | 是 | `"online"` 或 `"offline"` |
| `timestamp` | float | 是 | 事件时间戳（Unix 秒） |
| `snapshot` | object \| null | 否 | 玩家快照数据 |

**offline 响应** — 200 OK:

```json
// 首次离线
{"status": "scheduled", "user_id": "player_12345", "flow_run_id": "flow-a1b2c3d4e5f67890"}

// 重复离线（去重）
{"status": "debounced", "user_id": "player_12345"}
```

**online 响应** — 200 OK:

```json
{"status": "cancelled", "user_id": "player_12345"}
```

**错误响应**:

| 状态码 | 说明 |
|--------|------|
| 400 | `event_type` 不是 `"online"` 或 `"offline"` |

**示例**:

```bash
# 玩家离线
curl -X POST http://localhost:8000/webhooks/player-event \
  -H "Content-Type: application/json" \
  -H "X-API-Key: gap_your_api_key" \
  -d '{
    "user_id": "player_12345",
    "event_type": "offline",
    "timestamp": 1744100000.0,
    "snapshot": {
      "level": 85,
      "pvp_rating": 2400,
      "play_hours": 1250.5
    }
  }'

# 玩家上线
curl -X POST http://localhost:8000/webhooks/player-event \
  -H "Content-Type: application/json" \
  -H "X-API-Key: gap_your_api_key" \
  -d '{
    "user_id": "player_12345",
    "event_type": "online",
    "timestamp": 1744100300.0
  }'
```

---

## 分析结果

### GET /api/v1/analysis/{user_id}/latest

查询指定玩家的最新分析结果。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | string | 玩家 ID |

**成功响应** — 200 OK:

```json
{
  "user_id": "player_12345",
  "analyzed_at": "2026-04-13T15:30:00+00:00",
  "output": {
    "player_profile": {
      "playstyle": "competitive",
      "engagement_level": "high",
      "current_goal": ["提升PVP段位", "获取赛季奖励"],
      "bottlenecks": ["装备评分不足"]
    },
    "recommended_actions": [
      {
        "action_type": "pvp_arena",
        "priority": "high",
        "reason": "玩家PVP评分1800，推荐参与竞技场冲击段位奖励",
        "payload": {"arena_type": "ranked"}
      },
      {
        "action_type": "dungeon",
        "priority": "medium",
        "reason": "装备评分低于同段位平均水平",
        "payload": {"dungeon_id": "heroic_raid_05"}
      }
    ]
  }
}
```

**错误响应**:

| 状态码 | 说明 |
|--------|------|
| 404 | 未找到该玩家的分析结果 |

**示例**:

```bash
curl http://localhost:8000/api/v1/analysis/player_12345/latest \
  -H "X-API-Key: gap_your_api_key"
```

---

### GET /api/v1/analysis/{user_id}/history

查询指定玩家的分析历史记录。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | string | 玩家 ID |

**查询参数**:

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `limit` | int | 10 | 返回最多 N 条记录 |

**成功响应** — 200 OK:

```json
{
  "user_id": "player_12345",
  "count": 3,
  "history": [
    {
      "analyzed_at": "2026-04-13T15:30:00+00:00",
      "output": { "..." : "..." }
    },
    {
      "analyzed_at": "2026-04-12T10:00:00+00:00",
      "output": { "..." : "..." }
    },
    {
      "analyzed_at": "2026-04-11T22:15:00+00:00",
      "output": { "..." : "..." }
    }
  ]
}
```

**示例**:

```bash
curl "http://localhost:8000/api/v1/analysis/player_12345/history?limit=5" \
  -H "X-API-Key: gap_your_api_key"
```

---

## 租户管理

### POST /api/v1/tenants/register

注册新租户。此接口无需认证。

**请求体**:

```json
{
  "user_id": "my_game_alpha"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_id` | string | 是 | 租户关联的用户 ID（全局唯一） |

**成功响应** — 200 OK:

```json
{
  "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "api_key": "gap_a1b2c3d4e5f6789012345678",
  "user_id": "my_game_alpha"
}
```

**错误响应**:

| 状态码 | 说明 |
|--------|------|
| 409 | 该 `user_id` 已注册 |

**示例**:

```bash
curl -X POST http://localhost:8000/api/v1/tenants/register \
  -H "Content-Type: application/json" \
  -d '{"user_id": "my_game_alpha"}'
```

---

## 配额查询

### GET /api/v1/quota/usage

查询当前租户的配额使用情况。

**成功响应** — 200 OK:

```json
{
  "monthly_limit": 40000000,
  "used": 12500000,
  "remaining": 27500000,
  "usage_percent": "31.3%",
  "period_start": "2026-04-01",
  "period_end": "2026-05-01"
}
```

**示例**:

```bash
curl http://localhost:8000/api/v1/quota/usage \
  -H "X-API-Key: gap_your_api_key"
```

---

## LLM 提供商管理（管理员）

以下端点需要管理员权限（`is_admin=True` 的租户）。

### GET /api/v1/providers

列出所有 LLM 提供商。响应中隐藏了 `api_key`。

**成功响应** — 200 OK:

```json
[
  {
    "id": "uuid-xxx",
    "name": "DeepSeek 主力",
    "provider": "deepseek",
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
    "weight": 3,
    "is_active": true,
    "model_type": "default",
    "provider_type": "deepseek",
    "max_tokens": null,
    "timeout": 60,
    "extra_params": {},
    "created_at": "2026-04-05T10:00:00+00:00",
    "updated_at": "2026-04-05T10:00:00+00:00"
  }
]
```

**错误响应**:

| 状态码 | 说明 |
|--------|------|
| 403 | 非管理员租户 |

---

### POST /api/v1/providers

添加新的 LLM 提供商。

**请求体**:

```json
{
  "name": "DeepSeek 快速",
  "provider": "deepseek",
  "model": "deepseek-chat",
  "api_key": "sk-your-key",
  "base_url": "https://api.deepseek.com",
  "weight": 1,
  "model_type": "fast",
  "provider_type": "deepseek",
  "timeout": 30
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `name` | string | 是 | - | 名称 (1-100 字符) |
| `provider` | string | 是 | - | 提供商标识 (1-50 字符) |
| `model` | string | 是 | - | 模型名 (1-100 字符) |
| `api_key` | string | 是 | - | API 密钥 (1-500 字符) |
| `base_url` | string | 是 | - | API 地址 (1-500 字符) |
| `weight` | int | 否 | 1 | 轮询权重 (>0) |
| `model_type` | string | 否 | `"default"` | `"default"` 或 `"fast"` |
| `provider_type` | string | 否 | `"openai"` | `openai\|anthropic\|deepseek\|qwen\|zhipu\|grok` |
| `max_tokens` | int \| null | 否 | null | 最大生成 Token (1-1000000) |
| `timeout` | int | 否 | 60 | 超时秒数 (1-600) |
| `extra_params` | object | 否 | `{}` | 额外参数 |

**成功响应** — 201 Created（同 GET 列表中的对象结构）

---

### PUT /api/v1/providers/{provider_id}

更新 LLM 提供商（部分更新，只传需要修改的字段）。

**路径参数**: `provider_id` (UUID)

**请求体** (所有字段可选):

```json
{
  "weight": 5,
  "is_active": true
}
```

**成功响应** — 200 OK（同 GET 列表中的对象结构）

**错误响应**:

| 状态码 | 说明 |
|--------|------|
| 400 | 没有需要更新的字段 |
| 404 | Provider 不存在 |

---

### DELETE /api/v1/providers/{provider_id}

软删除 LLM 提供商（设为 `is_active=false`）。

**路径参数**: `provider_id` (UUID)

**成功响应** — 204 No Content（无响应体）

**错误响应**:

| 状态码 | 说明 |
|--------|------|
| 404 | Provider 不存在 |

---

## 健康检查

### GET /health

**无需认证**。

**响应** — 200 OK:

```json
{"status": "ok", "version": "2.0.0"}
```
