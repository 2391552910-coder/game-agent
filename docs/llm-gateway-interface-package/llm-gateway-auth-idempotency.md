---
doc_type: brainstorm
status: confirmed
summary: Gateway <-> LLM runtime HTTP 的通用鉴权、幂等、重试和 ID 规则。
---

# LLM Gateway Auth & Idempotency

这份文档记录所有 runtime HTTP 接口共享的通用规则。事件、决策、只读 Query 都引用这里，不在各自文档里重复展开。

## 已确认边界

- v1 runtime 接口只定义 HTTP `POST`。
- 生产环境必须使用 HTTPS；本地开发或内网联调可以按部署配置例外使用 HTTP。
- 请求 `Content-Type` 必须是 `application/json`，允许 `application/json; charset=utf-8`。
- 请求 body 必须是 UTF-8 JSON，不支持 form、multipart、plain text 或字符串化 JSON。
- 主 POST 接口不使用 query 参数。
- 所有 runtime HTTP 接口都使用同一套 HMAC header。
- 不额外交互 LLM token。
- `decisionLeaseId` 是业务许可，不是认证 token。

## Header

| Header | 说明 |
|---|---|
| `X-AppId` | 调用方 AppId。 |
| `X-TimestampMs` | 毫秒时间戳。 |
| `X-RequestId` | HTTP 请求幂等 ID。 |
| `X-Signature` | HMAC-SHA256 签名 hex。 |

### Header 规则

- `X-AppId` 必填，必须是非空字符串，建议格式为 `[A-Za-z0-9._:-]{1,128}`。
- 接收方使用 `X-AppId` 查找对应 `appSecret`；找不到或身份不匹配时返回 HTTP `401 signature_invalid`。
- v1 每个 `X-AppId` 只配置一个当前 `appSecret`；不定义双密钥、previous secret 或热轮换规则。
- 密钥变更按双方部署流程同步切换，不进入 runtime 接口字段。
- Gateway 调 LLM 事件接口、LLM 调 Gateway 决策接口应分别配置不同的 `X-AppId/appSecret`，两个方向不共用密钥。
- 协议字段仍然只有 `X-AppId`；调用方向身份由部署配置区分。
- `X-AppId` 不进入 JSON body。
- `X-TimestampMs` 必须是非空整数毫秒时间戳字符串，参与签名；同一次 HTTP 请求重试时必须保持不变。
- `X-TimestampMs` 缺失、空字符串、非整数或格式非法时，按请求级错误处理；超出时间窗时返回 HTTP `401 timestamp_expired`。
- `X-RequestId` 由调用方生成，必须是非空字符串，建议格式为 `[A-Za-z0-9._:-]{1,128}`。
- 同一次 HTTP 请求重试时，`X-RequestId` 必须保持不变。
- `X-RequestId` 缺失、空字符串或格式非法时，按请求级错误处理。
- `X-RequestId` 不进入 JSON body。
- `X-Signature` 必填，必须是 64 位小写 hex 字符串，不带 `sha256=` 前缀。
- `X-Signature` 缺失、长度错误、包含非小写 hex 字符、带前缀或验签不匹配时，返回 HTTP `401 signature_invalid`。

## 签名

签名前先计算请求体 SHA256：

```text
bodySha256Hex = sha256(raw HTTP request body bytes).hexLower()
```

签名文本：

```text
method + "\n" + path + "\n" + timestampMs + "\n" + requestId + "\n" + bodySha256Hex
```

签名算法：

```text
signature = hmac_sha256(appSecret, signingText).hexLower()
```

字段规范：

| 字段 | 规则 |
|---|---|
| `method` | HTTP 方法大写，例如 `POST`。 |
| `path` | URL path，不含 scheme、host、query；必须以 `/` 开头。 |
| `timestampMs` | 与 `X-TimestampMs` 完全一致的字符串。 |
| `requestId` | 与 `X-RequestId` 完全一致的字符串。 |
| `bodySha256Hex` | 对原始 HTTP request body bytes 计算 SHA256，小写 hex；JSON body 必须使用 UTF-8 编码，不做 JSON 规范化或重新序列化。 |

`bodySha256Hex` 只作为接收方本地计算值，用于 HMAC 签名校验和幂等判断，不作为 JSON body 字段传输。

## 路由约束

- `/api/gateway/events`、`/api/v1/hosting/llm/decision`、后续 `/api/v1/hosting/llm/query` 都只定义 `POST`。
- 非 `POST` 请求不进入事件或决策业务处理；HTTP 状态可按框架返回 `400 bad_request` 或 `405 Method Not Allowed`。
- 请求 `Content-Type` 缺失或不是 JSON 时，返回 HTTP `400 bad_request`。
- 收到 query 参数时，接收方可以直接按请求级错误拒绝，返回 HTTP `400 bad_request`。
- Gateway 和 LLM 都应配置 request body size limit；具体大小走服务配置，不进入接口字段。
- body 超过限制时，不进入业务处理，返回请求级错误；HTTP 状态可用 `400 bad_request` 或 `413 Payload Too Large`。
- v1 runtime body 不承载大对象；skill 大参数、长文本、截图、日志和 prompt 不应塞进事件或 `/decision` body。

## 请求级错误

| 情况 | 建议 HTTP | 是否进入业务 |
|---|---|---|
| 缺少 HMAC header | `401` | 否 |
| `X-AppId` 不匹配 | `401` | 否 |
| 签名不匹配 | `401` | 否 |
| 时间戳超出窗口 | `401` | 否 |
| JSON 解析失败 | `400` | 否 |
| 协议级必填字段缺失或类型错误 | `400` | 否 |
| 过载或限流 | `429` | 否 |
| 内部异常 | `500` | 否 |

## 错误响应

```json
{
  "error": {
    "code": "signature_invalid",
    "message": "request signature invalid"
  }
}
```

`error.code` 只取以下四个：

| `error.code` | `error.message` |
|---|---|
| `bad_request` | `bad request` |
| `signature_invalid` | `request signature invalid` |
| `timestamp_expired` | `request timestamp expired` |
| `internal_error` | `internal error` |

请求级错误响应不返回字段路径、签名计算细节、异常堆栈或动态业务详情；这些信息只写内部日志。

## ID 角色

| 字段 | 生成方 | 出现位置 | 作用 |
|---|---|---|---|
| `traceId` | Gateway | Gateway -> LLM 事件 envelope；双方日志 | 追踪一条“事件 -> 决策 -> 后续事件”的业务链路。 |
| `gatewayId` | Gateway | Gateway -> LLM 事件 envelope | 标识 Gateway 实例。 |
| `eventId` | Gateway | Gateway -> LLM 单个 event | 事件幂等去重。 |
| `decisionId` | LLM | `/decision` 请求和后续相关事件 | 决策幂等去重。 |
| `decisionLeaseId` | Gateway | Gateway -> LLM 事件顶层；`/decision` 请求带回 | 一次性决策许可。 |
| `skillCallId` | Gateway | `/decision` 响应；skill 相关事件 | 追踪一次已被接受的 skill 执行。 |
| `X-RequestId` | HTTP 调用方 | 双向 HTTP header | 单次 HTTP 请求幂等和 HMAC 签名材料，不替代业务 ID。 |

简化理解：`X-RequestId` 是 transport id，`eventId` 是 event id，`decisionId` 是 decision id，三者不能互相替代。

### ID 规则

- 所有业务 ID 都按 opaque string 处理，包括 `traceId / gatewayId / eventId / decisionId / decisionLeaseId / skillCallId / sessionId / accountId / roleId`。
- 即使底层来源是 long 或纯数字字符串，runtime JSON 里也必须作为 string 传输，不允许作为 JSON number。
- 接收方不能把业务 ID 解析成数字用于计算或再序列化；只能原样保存、比较和回传。
- `sessionId` 只对应一次托管生命周期，不回收、不复用；`session_stopped` 之后重新托管、重新登录或重开 session 必须生成新的 `sessionId`。
- `eventId / decisionLeaseId / skillCallId` 由 Gateway 保证全局唯一，不按 session 复用。
- `decisionId` 由 LLM 保证全局唯一，不按 session 复用。
- `traceId` 用于链路追踪，可以在同一条业务链路的多个事件和日志里复用；`traceId` 不是幂等键，也不承担唯一业务对象标识。
- `traceId` 由 Gateway 生成，必须是非空字符串，建议格式为 `[A-Za-z0-9._:-]{1,128}`。
- 同一个事件重试时，`traceId` 必须保持不变；LLM 调 `/decision` 时不需要回传 `traceId`。
- `gatewayId` 是 Gateway 实例唯一标识，必须是非空字符串，建议格式为 `[A-Za-z0-9._:-]{1,128}`。
- `gatewayId` 只用于日志、排查和识别来源，不参与 LLM 业务决策。
- `eventId` 由 Gateway 生成，必须是非空字符串，建议格式为 `[A-Za-z0-9._:-]{1,128}`。
- `eventId` 是 LLM 侧事件幂等主键；LLM 只按 `eventId + bodySha256` 去重，不按 `gatewayId + eventId` 组合去重。
- 同一个事件重试时，`eventId` 必须保持不变。
- `eventId` 缺失、类型错误、空字符串、过长或包含不适合日志/存储的字符时，LLM 返回 HTTP `400 bad_request`。
- LLM 收到重复 `eventId` 且 `bodySha256` 一致时，返回 `status=duplicate`；这里的 `bodySha256` 指 raw HTTP request body bytes 的 SHA256，不做 JSON 规范化。
- LLM 收到重复 `eventId` 但 `bodySha256` 不同时，应按异常处理，不要当作 `duplicate`。
- `decisionId` 是 LLM 生成的业务幂等键，必须全局唯一，不按 session 缩小作用域。
- `decisionId` 必须是非空字符串，建议格式为 `[A-Za-z0-9._:-]{1,128}`。
- `decisionId` 缺失、类型错误、空字符串、过长或包含不适合日志/存储的字符时，按请求级错误处理，返回 HTTP `400 bad_request`。
- `decisionLeaseId` 是 Gateway 发放的一次性决策许可，必须由 LLM 原样带回。
- `decisionLeaseId` 必须是非空字符串，建议格式为 `[A-Za-z0-9._:-]{1,128}`。
- `decisionLeaseId` 缺失、类型错误、空字符串、过长或包含不适合日志/存储的字符时，按请求级错误处理，返回 HTTP `400 bad_request`。
- `skillCallId` 由 Gateway 生成，必须是非空字符串，建议格式为 `[A-Za-z0-9._:-]{1,128}`。
- `X-RequestId` 由调用方生成，必须是非空字符串，建议格式为 `[A-Za-z0-9._:-]{1,128}`。
- 同一次 HTTP 请求重试时，`X-RequestId` 必须保持不变。

## 重试与幂等

- Gateway -> LLM 事件重试时，`eventId + bodySha256` 必须保持不变；Gateway 应复用完全相同的 JSON body bytes。
- Gateway -> LLM 事件推送遇到 HTTP `429`、HTTP `500`、网络超时或连接失败时应重试；重试次数和间隔由 Gateway 内部配置决定，不进入接口字段。
- Gateway -> LLM 事件重试只能更换 HTTP header，不得重新生成新的 `eventId`，也不得重新序列化 body。
- Gateway -> LLM 事件推送遇到 HTTP `400` 或 `401` 时不重试，因为这类错误代表请求本身或鉴权本身有问题。
- Gateway -> LLM 事件的 client timeout 由 Gateway 内部配置决定，不进入接口字段；超时视为投递失败并按重试策略处理。
- LLM 对已处理过且 `bodySha256` 一致的重复 `eventId` 返回 `status=duplicate`，Gateway 应按成功处理。
- LLM 收到已存在的 `eventId` 但本次 `bodySha256` 与第一次不同，表示 Gateway 复用了事件 ID 但内容发生变化；LLM 不返回 `accepted/duplicate`，应返回 HTTP `400 bad_request`。
- Gateway 推送带 `decisionLeaseId` 的事件时，只有收到 LLM HTTP `200` 且 `status=accepted/duplicate`，才认为这次决策机会已交给 LLM。
- Gateway 推送事件失败重试期间，不生成新事件、不更换 `decisionLeaseId`。
- 如果 Gateway -> LLM 事件持续投递失败并达到 Gateway 内部重试上限，Gateway 应结束本次托管 session，本地状态进入 `Failed`，停止原因按 `session_stopped.stop.reason=runtime_error` 处理。
- 如果 Gateway 收到 LLM 对事件返回 HTTP `400 bad_request`，不重试该事件；如果该事件带 `decisionLeaseId` 且 LLM 未成功接收决策机会，Gateway 应结束本次托管 session，本地状态进入 `Failed`，停止原因按 `session_stopped.stop.reason=runtime_error` 处理。
- 如果最终 `session_stopped` 事件本身也投递失败，Gateway 继续按事件重试策略投递该终态事件；但本地 session 已经结束，不能继续保持 LLM 托管运行态。
- `session_stopped` 属于终态事件，投递失败时应尽力重试一段时间，但不要求无限重试；达到 Gateway 内部终态事件重试上限后，可以停止投递并记录严重日志或告警。
- `session_stopped` 投递失败或最终放弃投递，都不改变 Gateway 本地 session 已终态的事实，本地终态不可回滚。
- LLM -> Gateway 决策重试时，`decisionId + decisionLeaseId + bodySha256` 必须保持不变；LLM 应复用完全相同的 JSON body bytes，字段顺序、空格和数字格式都不应改变。
- 同一次 HTTP 请求重试时，`X-RequestId` 和 body 保持不变。
- 如果因为时间窗过期需要重新发起请求，应使用新的 `X-TimestampMs`，`X-RequestId` 可以换新，但 Gateway -> LLM 事件仍保持同一个 `eventId + bodySha256`，LLM -> Gateway 决策仍保持同一个 `decisionId + decisionLeaseId + bodySha256`。
- LLM 调 `/decision` 超时后，只能按原样幂等重试，不能换新的 `decisionId` 重新发起同一张 `decisionLeaseId` 的决策。
- 同一个 `decisionId + decisionLeaseId + bodySha256` 重试应得到幂等结果；这里的 `bodySha256` 指 raw HTTP request body bytes 的 SHA256，不做 JSON 规范化。
- 同一个 `decisionId` 对应不同 `decisionLeaseId` 或不同 `bodySha256` 是调用方 bug，应返回 `idempotency_key_conflict`。
- 同一个 `decisionLeaseId` 只能消费一次；同一个 `decisionId + decisionLeaseId + bodySha256` 的 HTTP 重试不算第二次消费，应返回第一次处理结果。

### 幂等记录保留

- 协议不在请求或响应里传幂等记录 TTL。
- 双方必须保证同一个 session 存活期间，相关幂等记录不能丢失。
- session 结束后，双方仍应保留一小段内部 TTL，用于处理迟到的 HTTP 重试；具体 TTL 取值走服务配置，不进入接口字段。
- Gateway 侧幂等记录至少覆盖 `decisionId + decisionLeaseId + bodySha256 -> 第一次处理结果`。
- LLM 侧幂等记录至少覆盖 `eventId + bodySha256 -> accepted/duplicate`。
- Gateway 收到幂等记录已过期的旧 `/decision` 时，按当前 lease/session 状态处理，通常返回 `status=rejected / reason=lease_expired`。
- LLM 收到幂等记录已过期的旧事件时，如果无法确认曾经处理过，不能安全返回 `duplicate`；应按当前请求重新校验和处理。

## 额外约束

- `decisionLeaseId` 只表达“当前 session 允许消费一次决策”，不是认证 token。
- JSON 字段名必须按文档字面量严格匹配，不做大小写兼容或命名风格兼容。
- 例如只接受 `decisionLeaseId / contractVersion / eventType / occurredAtMs / skillCallId`，不接受 `decision_lease_id / DecisionLeaseId / occurred_at_ms` 等别名。
- 字段名错误等同于缺少正确字段且多传未知字段；LLM -> Gateway `/decision` 返回 `status=rejected / reason=schema_invalid`，Gateway -> LLM 事件返回 HTTP `400 bad_request`。
- string 字段不做自动 trim；ID、枚举、`skillName`、`schemaVersion` 等协议字符串必须直接传合法值。
- 空字符串、全空白字符串、带不应存在的前后空格，都按非法值处理；LLM -> Gateway `/decision` 返回 `status=rejected / reason=schema_invalid`，Gateway -> LLM 事件返回 HTTP `400 bad_request`。
- 所有枚举值都必须按文档字面量大小写严格匹配，不做大小写兼容或别名兼容。
- LLM -> Gateway `/decision` 里枚举值大小写错误时，返回 `status=rejected / reason=schema_invalid`；Gateway -> LLM 事件里枚举值大小写错误时，返回 HTTP `400 bad_request`。
- 所有 integer number 字段都必须使用普通十进制整数 JSON 数字写法，不允许科学计数法、小数点或前导 `+`；例如 `occurredAtMs / sceneId / waitMs`。
- HMAC 只保证调用方可信和请求未被篡改，不负责隐藏 body 内容；生产环境传输加密由 HTTPS 保证。
- 不在请求或响应里增加 `isSecure` 这类字段；传输安全由部署层保证。
- `gatewayVersion`、`buildVersion`、LLM 服务版本等部署版本信息写入双方日志或部署元数据，不进入 runtime JSON body。
- AppSecret、账号 password、client token、hosting token、raw prompt、raw response 和完整 raw body 都不应写入日志。
