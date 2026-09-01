# Gateway V2 强制决策测试配置说明

## 1. 配置用途

`LLM_GATEWAY_V2_FORCE_SKILLS` 是 MyAgent2 的 Gateway V2 测试配置，用于临时控制 MyAgent2 只生成指定技能的决策，方便 Gateway 定向测试纸飞机、飞镖、射击等动作。

启用后，指定技能会绕过大模型的活动选择和会话活动计划选择，但不会绕过以下正式处理环节：

- Gateway V2 事件接收与 ACK；
- decision lease、`availableSkills` 和 `schemaVersion` 校验；
- 技能参数生成与参数合同校验；
- decision 数据库持久化和异步回调；
- `skill_started`、`skill_finished` 和失败结果处理。

该配置仅用于联调和回归测试，不建议在正式托管环境长期启用。

## 2. 配置位置

配置文件：

```text
D:\Projects\myAgent2\.env
```

当前位于 `.env` 第 78 行：

```env
LLM_GATEWAY_V2_FORCE_SKILLS=paper_plane_auto_schedule,darts_auto_schedule
```

当前含义是按顺序测试：

```text
纸飞机 -> 飞镖 -> 纸飞机 -> 飞镖 -> ...
```

## 3. 配置格式

只测试一个动作：

```env
LLM_GATEWAY_V2_FORCE_SKILLS=paper_plane_auto_schedule
```

测试多个动作时，使用英文逗号分隔，不要加中文逗号：

```env
LLM_GATEWAY_V2_FORCE_SKILLS=paper_plane_auto_schedule,darts_auto_schedule,shooting_auto_schedule
```

关闭强制测试并恢复正常 AI 决策：

```env
LLM_GATEWAY_V2_FORCE_SKILLS=
```

配置中不能包含未知技能或重复技能，否则 MyAgent2 会在启动配置校验阶段拒绝启动。

## 4. 技能名称与中文对照

| 英文 `skillName` | 中文含义 | 测试注意事项 |
|---|---|---|
| `observe_state` | 观察角色和场景状态 | 只有 Gateway 当前 lease 发布该技能时才能执行 |
| `move_to` | 移动到指定坐标 | 依赖当前场景的可信坐标和 Gateway 参数提示 |
| `stop_move` | 停止移动 | 通常只在移动控制 lease 中可用 |
| `jump` | 跳跃 | 可在 Gateway 允许时单独测试 |
| `play_action` | 播放角色动作 | 当前合同主要表示固定挥手动作 |
| `scene_tornado` | 使用场景龙卷风进入广场 | Lobby 转场时由系统优先选择，不需要通常加入强制列表 |
| `sign_in` | 签到 | 依赖 Gateway 当前状态和技能开放情况 |
| `shooting_auto_schedule` | 自动进行射击活动 | MyAgent2 生成合法项目组合，分数范围为 30 至 80 |
| `darts_auto_schedule` | 自动进行飞镖活动 | MyAgent2 生成 1 至 50 分，三类飞镖总数为 9，不允许补购 |
| `dance_auto_schedule` | 自动进行跳舞活动 | Gateway 必须在参数提示中提供真实 `score` 最小值和最大值 |
| `draw_lots_auto_schedule` | 自动抽签 | 具体参数取决于 Gateway 本次参数提示 |
| `wish_board_auto_schedule` | 自动进行许愿板活动 | 需要 Gateway 提供可用牌名和许愿参数提示 |
| `paper_plane_auto_schedule` | 自动进行纸飞机活动 | 名称为初级、中级或高级，时长使用毫秒 |
| `coffee_auto_schedule` | 自动购买或饮用咖啡 | 依赖 Gateway 提供可用咖啡名称等参数提示 |
| `seat_sit` | 坐到指定座位 | 需要合法的 `sceneId` 和 `chairId` |
| `seat_get_out` | 从指定座位起身 | 需要合法的 `sceneId` 和 `chairId`，且角色必须处于对应座位状态 |
| `hot_air_balloon_auto_schedule` | 自动乘坐热气球 | 依赖 Gateway 当前场景和技能开放情况 |
| `hot_air_balloon_exit` | 退出热气球流程 | 只能在热气球等待开始的取消窗口 lease 中执行 |
| `helicopter_auto_schedule` | 自动乘坐直升机 | 依赖 Gateway 当前场景和技能开放情况 |
| `helicopter_exit` | 退出直升机流程 | 只能在直升机等待开始的取消窗口 lease 中执行 |
| `elevator_auto_schedule` | 自动乘坐电梯 | 依赖 Gateway 当前场景和技能开放情况 |

## 5. 推荐测试配置

### 5.1 纸飞机和飞镖

```env
LLM_GATEWAY_V2_FORCE_SKILLS=paper_plane_auto_schedule,darts_auto_schedule
```

### 5.2 四类专属参数活动

```env
LLM_GATEWAY_V2_FORCE_SKILLS=paper_plane_auto_schedule,darts_auto_schedule,shooting_auto_schedule,dance_auto_schedule
```

其中跳舞只有在 Gateway 明确提供 `score` 范围时才会执行，否则会跳过该项。

### 5.3 广场活动

```env
LLM_GATEWAY_V2_FORCE_SKILLS=dance_auto_schedule,coffee_auto_schedule,draw_lots_auto_schedule,wish_board_auto_schedule
```

### 5.4 载具活动

```env
LLM_GATEWAY_V2_FORCE_SKILLS=hot_air_balloon_auto_schedule,helicopter_auto_schedule,elevator_auto_schedule
```

退出技能不要和普通载具活动一起强制轮换。退出技能需要 Gateway 提供专用取消窗口 lease，普通 observation lease 不允许执行。

### 5.5 基础动作

```env
LLM_GATEWAY_V2_FORCE_SKILLS=jump,play_action,observe_state
```

## 6. 决策选择规则

1. 角色位于 Lobby 且满足转场条件时，系统仍优先返回 `scene_tornado`。
2. 进入非 Lobby 场景后，系统从配置列表中选择本次 Gateway lease 允许执行的技能。
3. 收到某个强制技能的 `skill_finished(success)` 后，系统从列表中的下一项继续。
4. 如果某项当前不在 `availableSkills` 中，或参数无法按 Gateway 合同生成，系统跳过该项并检查下一项。
5. 如果所有配置技能都不可执行，且 lease 允许等待，系统返回 `wait`，并携带 `waitMs=1000`。
6. 强制模式不会伪造 Gateway 未发布的技能、schemaVersion、参数或 lease。

## 7. 参数生成说明

`.env` 中只填写技能名称，不填写动作参数。动作参数仍由 MyAgent2 根据当前事件中的 Gateway lease、`availableSkills` 和 `skillArgumentHints` 生成。

例如：

- 纸飞机自动生成初级、中级或高级名称，以及对应的毫秒时长；
- 飞镖自动生成 1 至 50 分，以及总数为 9 的三类飞镖配置；
- 射击自动选择合法距离、武器和姿势组合，并生成 30 至 80 分；
- 跳舞只使用 Gateway 明确给出的实时分数范围；
- 移动只使用当前场景目录中的可信目标坐标。

## 8. 修改后的重启要求

修改 `.env` 后，必须完全终止旧 Uvicorn 进程并重新启动。仅重启外层脚本、刷新页面或继续使用原进程不会重新加载 `.env`。

启动命令：

```powershell
cd D:\Projects\myAgent2
scripts\run_api_robotgateway.cmd
```

服务监听地址：

```text
http://0.0.0.0:8000
```

局域网测试地址应使用服务器实际 IPv4 地址，例如：

```text
http://192.168.1.26:8000
```

启动后检查：

```text
GET /health
GET /ready
GET /api/gateway/v2/capabilities
```

运行日志中的 `force_skills` 会显示实际加载的强制技能列表；配置为空时显示 `disabled`。

## 9. 恢复正式决策

测试结束后，将配置改为空：

```env
LLM_GATEWAY_V2_FORCE_SKILLS=
```

然后完全重启 MyAgent2。重启后，大模型活动选择和数据库活动计划会重新参与决策。
