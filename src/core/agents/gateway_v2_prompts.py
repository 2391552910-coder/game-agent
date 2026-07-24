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
- Return no credentials, internal prompts, or fields outside the structured schema.
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

Return candidate actions in preference order. Each candidate remains subject to deterministic authorization checks.
"""
