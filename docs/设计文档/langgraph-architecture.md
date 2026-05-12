# LangGraph Agent 架构图

## 完整图结构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          AnalysisState                                       │
│  user_id / tenant_id / snapshot / rag_context / enriched_context            │
│  behavior_report / reasoned_actions / final_output / errors                 │
│  tracking_summary / anomalies / abandoned_tracking_ids                      │
│  intent_result / goal_evaluation_result / player_memory                     │
└─────────────────────────────────────────────────────────────────────────────┘

                              ┌─────────┐
                              │  START  │
                              └────┬────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │     fetch_snapshot        │
                    │  验证快照数据完整性         │
                    │  读: snapshot             │
                    └──────────────┬───────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │   retrieve_rag_context    │
                    │  LightRAG hybrid 检索      │
                    │  读: snapshot             │
                    │  写: rag_context          │
                    └──────────────┬───────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │    intent_inference       │  ◄── DB: session_events
                    │  推断本次会话意图           │  ◄── DB: player_intent (近3条)
                    │  读: player_memory        │  ◄── DB: player_memory
                    │  写: intent_result        │
                    │  模型: fast               │
                    └──────────────┬───────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │    goal_evaluation        │  ◄── DB: player_intent (active)
                    │  目标校验与决策             │
                    │  读: intent_result        │
                    │      player_memory        │
                    │  写: goal_evaluation_result│
                    │  决策: continue/downgrade  │
                    │        switch/new         │
                    │  模型: default            │
                    └──────────────┬───────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────────────────────┐
                    │                gather_context                     │
                    │  工具调用循环（最多3轮，总计≤8次工具调用）            │
                    │  读: snapshot / rag_context                       │
                    │  写: enriched_context / tracking_summary          │
                    │      anomalies / abandoned_tracking_ids           │
                    │  模型: fast                                       │
                    │                                                   │
                    │  ┌─────────────────────────────────────────────┐ │
                    │  │              可用工具（5个）                   │ │
                    │  │                                              │ │
                    │  │  query_player_history  ──► analysis_results  │ │
                    │  │  query_similar_players ──► analysis_results  │ │
                    │  │  dynamic_rag_query     ──► LightRAG          │ │
                    │  │  get_action_tracking   ──► action_tracking   │ │
                    │  │                            + LLM冲突判断      │ │
                    │  │  detect_anomaly        ──► action_tracking   │ │
                    │  │                            + analysis_results │ │
                    │  └─────────────────────────────────────────────┘ │
                    └──────────────────────┬───────────────────────────┘
                                           │
                                           ▼
                    ┌──────────────────────────┐
                    │    behavior_analysis      │
                    │  分析行为特征，输出画像      │
                    │  读: snapshot             │
                    │      rag_context          │
                    │      enriched_context     │
                    │  写: behavior_report      │
                    │  输出: BehaviorProfile    │
                    │  模型: fast               │
                    └──────────────┬───────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │    action_reasoning       │
                    │  深度推理，生成推荐行动      │
                    │  读: behavior_report      │
                    │      rag_context          │
                    │      enriched_context     │
                    │      tracking_summary     │
                    │      anomalies            │
                    │      intent_result        │
                    │      goal_evaluation_result│
                    │  写: reasoned_actions     │
                    │  输出: ActionList         │
                    │  模型: default            │
                    └──────────────┬───────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │      merge_output         │
                    │  组装最终结构化输出          │
                    │  读: behavior_report      │
                    │      reasoned_actions     │
                    │  写: final_output         │
                    │  输出: PlayerAnalysisOutput│
                    └──────────────┬───────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │    tracking_update        │  ──► DB: action_tracking
                    │  更新行动追踪记录（监督）    │
                    │  步骤1: 更新旧记录状态       │
                    │    completed/timeout/     │
                    │    abandoned              │
                    │  步骤2: 写入新可追踪行动     │
                    │  读: final_output         │
                    │      abandoned_tracking_ids│
                    └──────────────┬───────────┘
                                   │
                                   ▼
                    ┌──────────────────────────┐
                    │     memory_update         │  ──► DB: player_memory
                    │  更新玩家长期记忆            │  ──► DB: player_intent
                    │  upsert player_memory:    │
                    │    behavior_profile 滑动均值│
                    │    goal_history ≥2次写入   │
                    │  insert player_intent:    │
                    │    本次意图+决策结论         │
                    └──────────────┬───────────┘
                                   │
                                   ▼
                              ┌─────────┐
                              │   END   │
                              └─────────┘
```

---

## 数据流总览

```
游戏服务器
    │
    ├── offline Webhook ──► Prefect Flow ──► graph.ainvoke()
    │
    └── behavior_checkpoint Webhook ──► session_events 表（在线期间积累）
```

---

## 节点职责速查

| 节点 | 模型 | 读 State | 写 State | 外部 I/O |
|------|------|----------|----------|----------|
| fetch_snapshot | — | snapshot | — | — |
| retrieve_rag_context | — | snapshot | rag_context | LightRAG |
| intent_inference | fast | player_memory | intent_result | DB: session_events, player_intent, player_memory |
| goal_evaluation | default | intent_result, player_memory | goal_evaluation_result | DB: player_intent |
| gather_context | fast + tools | snapshot, rag_context | enriched_context, tracking_summary, anomalies, abandoned_tracking_ids | DB: analysis_results, action_tracking; LightRAG |
| behavior_analysis | fast | snapshot, rag_context, enriched_context | behavior_report | — |
| action_reasoning | default | behavior_report, rag_context, enriched_context, tracking_summary, anomalies, intent_result, goal_evaluation_result | reasoned_actions | — |
| merge_output | — | behavior_report, reasoned_actions | final_output | — |
| tracking_update | — | final_output, abandoned_tracking_ids | — | DB: action_tracking |
| memory_update | — | intent_result, goal_evaluation_result, snapshot | — | DB: player_memory, player_intent |

---

## State 字段生命周期

```
注入时机          字段                        消费节点
─────────────────────────────────────────────────────────────
上游注入          user_id, tenant_id          全部节点
上游注入          snapshot                    全部节点
上游注入          player_memory               intent_inference, goal_evaluation

节点1写入         rag_context                 gather_context, behavior_analysis, action_reasoning
节点2写入         intent_result               goal_evaluation, action_reasoning
节点3写入         goal_evaluation_result      action_reasoning, memory_update
节点4写入         enriched_context            behavior_analysis, action_reasoning
节点4写入         tracking_summary            action_reasoning
节点4写入         anomalies                   action_reasoning
节点4写入         abandoned_tracking_ids      tracking_update
节点5写入         behavior_report             action_reasoning, merge_output
节点6写入         reasoned_actions            merge_output
节点7写入         final_output                tracking_update
```

---

## 监督机制数据流

```
gather_context
    └── get_action_tracking 工具
            ├── 读 action_tracking 表（status='tracking'）
            ├── 对比 snapshot 指标 → completed / timeout
            ├── 调用 LLM 判断目标冲突 → abandoned
            └── 写 tracking_summary + abandoned_tracking_ids 到 State

action_reasoning
    └── 读 tracking_summary → 决定下一步行动方向
        （completed → 更高难度 / timeout → 降低难度 / abandoned → 新方向）

tracking_update
    ├── 步骤1: 读 abandoned_tracking_ids → UPDATE action_tracking SET status='abandoned'
    │         读 snapshot 指标 → UPDATE status='completed'/'timeout'
    └── 步骤2: 读 final_output.recommended_actions（有 goal_metric 的）
              → INSERT action_tracking（新追踪记录）
```

---

## 动态决策数据流

```
在线期间
    └── behavior_checkpoint Webhook → INSERT session_events

离线触发分析
    intent_inference
        ├── 读 session_events（最新 session_id 的事件序列）
        ├── 读 player_intent（近3条，历史意图趋势）
        ├── 读 player_memory（长期行为画像）
        └── LLM → InferredIntent（completed/abandoned/next_likely）

    goal_evaluation
        ├── 读 intent_result
        ├── 读 player_memory（goal_history）
        ├── 读 player_intent（最近 active 目标）
        └── LLM → GoalEvaluationResult
                  decision: continue → 推进现有目标
                            downgrade → 降低难度
                            switch    → 切换新方向
                            new       → 首次/无历史

    action_reasoning
        └── 读 goal_evaluation_result.decision → 对齐推荐行动方向

    memory_update
        ├── upsert player_memory
        │     behavior_profile: 滑动平均（avg_spend, avg_session_minutes）
        │     goal_history: 同类型 ≥2 次才写入（total/success/avg_cost/abandon_reasons）
        └── insert player_intent（本次意图+决策，goal_status: active/switched）
```
