# PostgreSQL 数据库表说明

本文档整理当前项目使用的 PostgreSQL 表结构，包括业务表与 Alembic 系统表 `alembic_version`。

## 1. `tenants`

### 字段说明
- `id`：UUID，主键，默认 `gen_random_uuid()`，租户唯一标识。
- `user_id`：`varchar(255)`，非空，唯一，外部用户标识。
- `api_key`：`varchar(255)`，非空，唯一，租户认证密钥。
- `is_active`：`bool`，非空，默认 `true`，表示租户是否启用。
- `is_admin`：`bool`，非空，默认 `false`，表示是否为管理员租户。
- `created_at`：`timestamptz`，创建时间，默认当前时间。
- `updated_at`：`timestamptz`，更新时间，默认当前时间。

### 约束说明
- 主键：`id`
- 唯一约束：`user_id`
- 唯一约束：`api_key`

### 索引说明
- `ix_tenants_user_id`：按 `user_id` 查询租户。
- `ix_tenants_api_key`：按 `api_key` 进行认证查询。

---

## 2. `quotas`

### 字段说明
- `id`：UUID，主键，默认 `gen_random_uuid()`，配额记录标识。
- `tenant_id`：UUID，非空，外键指向 `tenants.id`，所属租户。
- `monthly_limit`：`bigint`，非空，每月额度上限。
- `used`：`bigint`，非空，默认 `0`，已使用额度。
- `period_start`：`date`，非空，周期开始日期。
- `period_end`：`date`，非空，周期结束日期。
- `created_at`：`timestamptz`，创建时间，默认当前时间。

### 约束说明
- 主键：`id`
- 外键：`tenant_id -> tenants.id`
- 删除规则：租户删除时级联删除配额记录
- 唯一约束：`uq_quotas_tenant_period (tenant_id, period_start)`

### 索引说明
- `ix_quotas_tenant_id`：按租户查询配额记录。

---

## 3. `analysis_results`

### 字段说明
- `id`：UUID，主键，默认 `gen_random_uuid()`，分析结果标识。
- `tenant_id`：UUID，非空，外键指向 `tenants.id`，结果所属租户。
- `user_id`：`varchar(255)`，非空，玩家标识。
- `snapshot_hash`：`varchar(64)`，非空，快照哈希。
- `output_json`：`JSON`，非空，分析输出全文。
- `analyzed_at`：`timestamptz`，非空，实际分析时间。
- `created_at`：`timestamptz`，创建时间，默认当前时间。

### 约束说明
- 主键：`id`
- 外键：`tenant_id -> tenants.id`
- 删除规则：租户删除时级联删除分析结果

### 索引说明
- `ix_analysis_results_tenant_user`：按租户 + 用户查询分析结果。
- `ix_analysis_results_user_id`：按玩家查询分析结果。

---

## 4. `llm_providers`

### 字段说明
- `id`：UUID，主键，默认 `gen_random_uuid()`，provider 记录标识。
- `name`：`varchar(100)`，非空，provider 显示名。
- `provider`：`varchar(50)`，非空，供应商标识。
- `model`：`varchar(100)`，非空，模型名称。
- `api_key`：`varchar(500)`，非空，API 密钥。
- `base_url`：`varchar(500)`，非空，API 基础地址。
- `weight`：`int`，非空，默认 `1`，负载均衡权重。
- `is_active`：`bool`，非空，默认 `true`，是否启用。
- `model_type`：`varchar(20)`，非空，默认 `default`，模型类型。
- `created_at`：`timestamptz`，创建时间，默认当前时间。
- `updated_at`：`timestamptz`，更新时间，默认当前时间。
- `provider_type`：`varchar(50)`，非空，默认 `openai`，提供商类别。
- `max_tokens`：`int`，可空，最大生成 token 数。
- `timeout`：`int`，非空，默认 `60`，请求超时时间。
- `extra_params`：`JSON`，非空，默认 `{}`，额外参数。

### 约束说明
- 主键：`id`

### 索引说明
- `ix_llm_providers_model_type`：按模型类型筛选。
- `ix_llm_providers_is_active`：查询启用中的 provider。
- `ix_llm_providers_provider_type`：按提供商类别筛选。

---

## 5. `action_tracking`

### 字段说明
- `id`：UUID，主键，默认 `gen_random_uuid()`，追踪记录标识。
- `tenant_id`：UUID，非空，租户 ID。
- `user_id`：`varchar(255)`，非空，玩家 ID。
- `analysis_id`：UUID，可空，关联的 `analysis_results` 记录 ID。
- `action_type`：`varchar(100)`，非空，行动类型。
- `action_desc`：`text`，可空，行动描述。
- `goal_metric`：`varchar(100)`，可空，完成判断指标名。
- `goal_value`：`float`，可空，目标值。
- `baseline_value`：`float`，可空，推荐时基准值。
- `expected_hours`：`int`，可空，预计完成所需小时数。
- `deadline`：`timestamptz`，可空，截止时间。
- `status`：`varchar(20)`，非空，默认 `tracking`，当前状态。
- `completed_at`：`timestamptz`，可空，完成时间。
- `completion_snapshot`：`JSON`，可空，完成时快照。
- `created_at`：`timestamptz`，非空，默认 `now()`，创建时间。
- `updated_at`：`timestamptz`，非空，默认 `now()`，更新时间。

### 约束说明
- 主键：`id`
- 该表未定义显式外键，`tenant_id` 与 `analysis_id` 为业务关联字段。

### 索引说明
- `ix_action_tracking_tenant_user`：按租户 + 用户查询追踪记录。
- `ix_action_tracking_status`：按租户 + 用户 + 状态查询进行中的追踪记录。
- `ix_action_tracking_created_at`：按租户 + 用户 + 创建时间查询最近记录。

---

## 6. `session_events`

### 字段说明
- `id`：UUID，主键，默认 `gen_random_uuid()`，事件记录标识。
- `tenant_id`：UUID，非空，租户 ID。
- `user_id`：`varchar(255)`，非空，玩家 ID。
- `session_id`：`varchar(64)`，非空，同一次在线期间的会话 ID。
- `event_type`：`varchar(100)`，非空，事件类型。
- `event_data`：`JSON`，可空，事件详细数据。
- `snapshot`：`JSON`，可空，事件发生时的玩家快照。
- `occurred_at`：`timestamptz`，非空，默认 `now()`，事件发生时间。
- `created_at`：`timestamptz`，非空，默认 `now()`，记录创建时间。

### 约束说明
- 主键：`id`
- 该表未定义显式外键。

### 索引说明
- `ix_session_events_tenant_user_session`：按租户 + 用户 + 会话查询事件序列。
- `ix_session_events_occurred_at`：按租户 + 用户 + 时间查询最近事件。

---

## 7. `player_intent`

### 字段说明
- `id`：UUID，主键，默认 `gen_random_uuid()`，意图记录标识。
- `tenant_id`：UUID，非空，租户 ID。
- `user_id`：`varchar(255)`，非空，玩家 ID。
- `session_id`：`varchar(64)`，可空，关联会话 ID。
- `inferred_intent`：`JSON`，可空，意图推断结果。
- `current_goal`：`text`，可空，当前主目标。
- `goal_type`：`varchar(100)`，可空，目标分类标签。
- `goal_status`：`varchar(20)`，非空，默认 `active`，目标状态。
- `goal_progress`：`float`，可空，目标完成度。
- `cost_expected`：`float`，可空，预期代价。
- `cost_actual`：`float`，可空，实际代价。
- `evaluation_result`：`varchar(20)`，可空，决策结论。
- `evaluation_reason`：`text`，可空，决策原因。
- `created_at`：`timestamptz`，非空，默认 `now()`，分析时间。

### 约束说明
- 主键：`id`
- 该表未定义显式外键。

### 索引说明
- `ix_player_intent_tenant_user`：按租户 + 用户查询意图历史。
- `ix_player_intent_created_at`：按租户 + 用户 + 创建时间查询最近分析。

---

## 8. `player_memory`

### 字段说明
- `id`：UUID，主键，默认 `gen_random_uuid()`，记忆记录标识。
- `tenant_id`：UUID，非空，租户 ID。
- `user_id`：`varchar(255)`，非空，玩家 ID。
- `behavior_profile`：`JSON`，可空，行为画像。
- `goal_history`：`JSON`，可空，目标历史统计。
- `analysis_count`：`int`，非空，默认 `0`，累计分析次数。
- `created_at`：`timestamptz`，非空，默认 `now()`，首次创建时间。
- `updated_at`：`timestamptz`，非空，默认 `now()`，最后更新时间。

### 约束说明
- 主键：`id`
- 唯一约束：`uq_player_memory_tenant_user (tenant_id, user_id)`

### 索引说明
- `ix_player_memory_tenant_user`：按租户 + 用户查询记忆记录，也常用于 upsert 定位。

---

## 9. `alembic_version`

这是 Alembic 的系统版本表，用于记录当前数据库迁移版本，不属于业务表。

### 字段说明
- `version_num`：`varchar(32)`，非空，当前生效的迁移版本号。

### 约束说明
- 主键：`version_num`
- 该表通常只有一条记录，用于表示数据库已迁移到哪个 revision。

### 索引说明
- 无额外业务索引。

