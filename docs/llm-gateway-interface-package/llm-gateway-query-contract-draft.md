---
doc_type: brainstorm-checkpoint
status: scratch
summary: LLM 向 Gateway 发起只读查询的后续讨论草案。
---

# LLM Gateway Query Contract Draft

这份文件只记录后续要单独讨论的只读查询接口问题，不作为当前 Gateway <-> LLM 主决策接口的已定实现。

## 已确认边界

- Query 是 LLM 向 Gateway 读取信息，不是执行动作。
- Query 使用和 `/decision` 相同的 HMAC header：`X-AppId / X-TimestampMs / X-RequestId / X-Signature`。
- Query 不使用 `decisionLeaseId`。
- Query 不消费当前 `decisionLeaseId`。
- Query 不生成新的 `decisionLeaseId`。
- Query 请求携带 `sessionId` 定位要查询的托管 session。
- Gateway 必须校验该 `sessionId` 是否存在，以及当前 LLM 调用方是否允许读取该 session。
- Query 首版只服务当前托管决策辅助，只允许查询 `Running` session。
- `sessionId` 不存在时返回 `session_not_found`。
- session 已 `Stopped` 或 `Failed` 时返回 `session_not_running`。
- 终态原因已通过 `session_stopped` 事件通知 LLM，不通过 Query 查询历史。
- Query 不打断正在执行的 skill。
- Query 不改变 session 状态。
- Query 只能读取 Gateway 当前已知或可安全查询的信息。
- 当前有 skill 正在执行时，Query 仍可返回当前快照或最近一次可用数据。
- Query 失败只影响本次查询，不影响正在执行的 skill。

## 候选接口

```text
POST /api/v1/hosting/llm/query
```

候选请求：

```json
{
  "contractVersion": "llm-gateway-http-v1",
  "queryId": "q-001",
  "sessionId": "session-001",
  "queryType": "nearby_roles",
  "arguments": {
    "radius": 20
  }
}
```

候选响应：

```json
{
  "status": "ok",
  "queryId": "q-001",
  "sessionId": "session-001",
  "result": {
    "roles": []
  }
}
```

## 响应状态

- Query 响应使用 `status=ok` / `status=failed`，不使用 `/decision` 的 `accepted/rejected`。
- `status=ok` 时返回 `result`。
- `status=failed` 时返回固定枚举 `reason`，不影响正在执行的 skill。
- 已确认的首批失败 reason：`session_not_found`、`session_not_running`。

## 后续需要逐项讨论

- 是否使用 `sessionId` 定位查询目标，还是也需要 Gateway 发放 query scope。
- `queryId` 幂等规则和格式。
- `queryType` 首批枚举。
- `arguments` 是否按 `queryType + schemaVersion` 维护版本。
- `result` 是否按 `queryType + schemaVersion` 维护结果契约。
- Query 是否允许返回最近缓存数据，以及如何表达数据新鲜度。
- Query 失败的 `status / reason` 枚举。
- Query 是否受限流保护。
- Query 和正在执行 skill 的并发读写边界。

## 并发边界

- `/decision` 是写操作，同一个 session 必须串行处理，一张 `decisionLeaseId` 只处理一次。
- `/query` 是只读操作，可以和正在执行的 skill 并发，也可以和 `/decision` 并发。
- `/query` 只能读取快照或 Gateway 可安全读取的信息，不能修改任何 Gateway 状态。
- 如果 `/query` 无法读取一致实时数据，可以返回最近可用快照或 query 失败，但不能等待、取消或打断正在执行的 skill。

## 候选 queryType

- `nearby_roles`：查询周围角色。
- `role_detail`：查询某个角色详情。
- `self_status`：查询自己更详细状态。
- `scene_objects`：查询场景对象。
- `inventory_snapshot`：查询背包快照。
