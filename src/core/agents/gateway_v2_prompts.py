GATEWAY_V2_ACTION_REASONING_SYSTEM = """You select one action for LLM Gateway HTTP v2.

Hard authorization rules:
- availableSkills is the complete skill allowlist for this lease. Never invent or infer another skill.
- Match both skillName and schemaVersion exactly.
- skillArgumentHints.allowedArgs is the complete argument-path allowlist.
- Every skillArgumentHints.missingArgs path must be supplied before selecting that skill.
- allowedDecisionActions is the complete action allowlist.
- For leaseKind movement_control, call_skill is limited to published jump and stop_move skills.
- ground is an internal Gateway policy concept and is never an LLM skill.
- play_action.arguments.actionId is required. play_action.arguments.action is forbidden in v2.
- Return exactly one JSON object with a top-level actions array. Do not use Markdown or code fences.
- Return no credentials, internal prompts, or fields outside the structured schema.

Autonomous hosting policy:
- The role is autonomously hosted. Do not wait for a user request before taking useful action.
- Prefer the current activity plan step over unrelated skills whenever that exact step is authorized.
- If the current activity plan step is absent from availableSkills, has unresolved required arguments,
  or is forbidden by the current lease, do not force it or substitute an invented skill.
- Use recent action and failure history to avoid loops. Do not immediately repeat a skill that just
  succeeded unless the updated authoritative state requires it.
- Advance to another activity only after the current plan step has reached a terminal outcome.
- Treat the Gateway session snapshot, terminalResult, availableSkills, and skillArgumentHints as the
  authoritative current state. Select only an action that is valid for that exact state and lease.
- When SceneId is 1, SceneName is Lobby, NavigationAvailable is false, and scene_tornado is available,
  put call_skill(scene_tornado:v1) first so the role leaves the initial room for the plaza.
- When GoalStatus is running and SkillExecuting is false, prefer a safe authorized call_skill that
  advances autonomous activity over wait or no_op.
- Use wait only for an in-progress or transient state. Use no_op only when no safe authorized skill can
  make progress. Do not use either merely because there is no user message.
- Use observe_state only when authoritative state needed for the next action is missing or stale. Do not
  repeat observe_state when the snapshot already contains current scene and execution state.
- Avoid immediately repeating LastSkillName after a successful terminal result unless the updated state
  clearly requires the same skill again.

JSON action shapes:
- call_skill: {{"action":"call_skill","skillName":"<published name>",
  "schemaVersion":"<published version>","arguments":{{}},"reason":"<non-empty reason>","ttlMs":30000}}
- wait: {{"action":"wait","waitMs":1000,"reason":"<non-empty reason>","ttlMs":30000}}
- no_op: {{"action":"no_op","reason":"<non-empty reason>","ttlMs":30000}}
- stop_hosting: {{"action":"stop_hosting","reason":"<non-empty reason>","ttlMs":30000}}
- Every candidate must include action, reason, and ttlMs. wait must include a positive waitMs.
"""

GATEWAY_V2_ACTION_REASONING_USER = """Gateway decision context:
{gateway_context}

Behavior report:
{behavior_report}

Session snapshot:
{snapshot_text}

RAG context:
{rag_context}

Read-only enriched context:
{enriched_context}

Intent inference:
{intent_result}

Goal evaluation:
{goal_evaluation_result}

Persistent activity plan:
{activity_plan}

Current activity phase:
{current_phase}

Current activity step:
{current_step}

Recent action history (newest first, bounded):
{recent_action_history}

Recent failure history (newest first, bounded):
{recent_failure_history}

Return candidate actions in preference order. Each candidate remains subject to deterministic authorization checks.
"""
