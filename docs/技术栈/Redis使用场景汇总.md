# Redis 使用场景汇总

## 一、核心基础设施

### 连接池管理
`src/core/infrastructure/redis.py` 提供统一的 Redis 连接池管理：

```python
async def init_redis() -> redis.Redis:
    """初始化 Redis 连接池。应用启动时调用。"""
    _pool = redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=50,
        socket_timeout=60,
        socket_connect_timeout=30,
        retry_on_timeout=True,
    )
```

---

## 二、使用场景

### 1. LightRAG 引擎
**文件**: `src/core/engine/lightrag_engine.py`

| 用途 | 存储类型 | 说明 |
|------|----------|------|
| KV 存储 | `RedisKVStorage` | 存储文本 chunks |
| 文档状态 | `RedisDocStatusStorage` | 追踪文档处理状态 |
| LLM 响应缓存 | - | 减少重复调用 |

### 2. 认证中间件
**文件**: `src/api/middleware.py` → `AuthMiddleware`

```python
# API Key 认证缓存
cache_key = f"auth_cache:{api_key}"
cached = await redis.get(cache_key)
if cached:
    tenant_info = json.loads(cached)
else:
    # PostgreSQL 验证后写入缓存
    await redis.setex(cache_key, 300, json.dumps(tenant_info))  # TTL 5分钟
```

### 3. 限流中间件
**文件**: `src/api/middleware.py` → `RateLimitMiddleware`

使用 **ZSET 滑动窗口** 实现限流：

```python
key = f"ratelimit:{tenant_id}:{client_ip}"
pipe = redis.pipeline()
pipe.zremrangebyscore(key, 0, window_start)  # 移除过期记录
pipe.zadd(key, {str(now): now})               # 添加当前请求
pipe.zcard(key)                               # 获取窗口内请求数
pipe.expire(key, self.window)
```

### 4. 分布式任务去重
**文件**: `src/core/scheduler/triggers.py`

使用 **SET NX** 原子操作实现离线分析任务去重：

```python
key = f"debounce:{tenant_id}:{user_id}"
set_ok = await redis.set(key, placeholder, ex=DEBOUNCE_TTL, nx=True)
if not set_ok:
    # 已有待处理任务，去重忽略
    return None
```

### 5. LLM 负载均衡器
**文件**: `src/core/llm/balancer.py`

追踪 provider 健康状态，用于熔断判断：

```python
# 报告失败
key = f"health:{provider_id}"
count = await redis.incr(key)
await redis.expire(key, 3600)

# 报告成功（重置计数）
await redis.delete(key)
```

---

## 三、配置信息

| 配置项 | 默认值 |
|--------|--------|
| Redis URL | `redis://localhost:6379/0` |
| 最大连接数 | 50 |
| 套接字超时 | 60秒 |
| 连接超时 | 30秒 |
| 内存限制 | `maxmemory 512mb` |
| 淘汰策略 | `allkeys-lru` |

---

## 四、数据流向

```
用户请求
    │
    ▼
┌──────────────┐
│  Redis 缓存  │ ← 快速路径：认证缓存、限流检查
└──────┬───────┘
       │ 未命中/需要深度检索
       ▼
┌─────────────────────────────────────┐
│  Milvus (向量) + Neo4j (图谱)       │ ← 深度检索
└──────┬──────────────────────────────┘
       │
       ▼
┌──────────────┐
│   LLM 生成   │
└──────────────┘
```

---

## 五、Key 命名规范

| 前缀 | 用途 | 示例 |
|------|------|------|
| `auth_cache:` | API Key 认证缓存 | `auth_cache:sk-xxx` |
| `ratelimit:` | 限流计数器 | `ratelimit:tenant-001:192.168.1.1` |
| `debounce:` | 任务去重 | `debounce:tenant-001:user-123` |
| `health:` | Provider 健康状态 | `health:openai-gpt4` |
| `text_chunks:` | 文档文本块 | `text_chunks:doc-abc:chunk-001` |

---

## 六、总结

Redis 在系统中扮演以下核心角色：

1. **缓存层**：减少数据库查询，提升响应速度
2. **分布式协调**：实现跨进程任务去重
3. **状态管理**：追踪认证状态、限流计数、服务健康度
4. **快速存储**：支撑 LightRAG 的 KV 存储需求
