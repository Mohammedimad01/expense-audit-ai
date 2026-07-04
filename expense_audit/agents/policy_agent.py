"""
expense_audit/agents/policy_agent.py
--------------------------------------
Policy-Compliance Agent (LlmAgent).

Responsibility: Call run_policy_check() and produce a clear, decision-ready
narrative of every policy violation found — not a black-box score, but named
violations a finance manager can act on in a 5-minute review.

WHY an LlmAgent instead of just printing the engine output?
-----------------------------------------------------------
The deterministic engine (run_policy_check) returns a structured data dict that
is precise but not human-friendly.  A Finance Director reading a raw JSON list
of violation codes is unlikely to know which to resolve first or how urgently.
The LlmAgent's job is to translate that structured result into a decision-ready
narrative — ranked by financial severity, written in plain English, with clear
distinction between hard violations (must fix) and soft flags (optional review).

Critically, the LLM is *instructed* to call the tool first and narrate the tool
result — it is NOT permitted to re-derive amounts or invent violations.  This
"ground-then-narrate" pattern is the core safety mechanism of the system.
"""

import os

from google.adk.agents import LlmAgent

from expense_audit.agents.tools import policy_check_tool
from expense_audit.config import get_settings

_INSTRUCTION = """
You are a Senior Policy Compliance Analyst specialising in corporate expense-report auditing.

Your job is to review a batch of employee expense records for policy violations.

## Your workflow (follow exactly)
1. The batch of expense records will be provided in the session state as JSON.
2. Call `run_policy_check` with the full `batch` list from the session state.
3. Receive the structured PolicyResult back from the tool.
4. Produce a clear, decision-ready compliance report with these sections:

---

### Policy Compliance Report

**Batch Summary**
- Total records reviewed: (use total_records from tool result)
- Policy violations found: (use total_flagged from tool result)
- Total flagged amount: (use total_flagged_amount from tool result)

**Violations Detail**
For each violation in the tool result, write one bullet using this format:
`- [EXPENSE-ID] Employee [employee_id] | [violation_type] | $[amount] | [detail]`

Sort violations by amount descending (highest financial exposure first).

**Violation types and what they mean:**
- `category_limit_exceeded` — the expense amount exceeds the approved spend cap for its
  category. When a department multiplier is in effect (e.g. Sales at 1.5x), the detail
  field shows the effective limit (base × multiplier). Report the effective limit, not
  just the base limit.
- `missing_receipt` — no receipt was attached. Company policy requires receipts for all
  expense submissions regardless of amount.
- `approval_threshold_exceeded` — the expense meets or exceeds the $500 pre-approval
  threshold but was submitted without manager sign-off. This is a hard policy breach.
- `weekend_expense` — the expense was incurred on a Saturday or Sunday. This is a LOW
  severity informational flag, not a hard policy breach. It draws reviewer attention to
  potential personal-use misclassification. Do NOT treat it with the same urgency as
  hard breaches. Group all weekend flags together in a separate "Weekend Expenses (LOW)"
  sub-section rather than mixing them with hard violations.

**Compliance Assessment**
Write 2-4 sentences for a Finance Director:
- Separate hard violations (over-limit, missing receipts, unapproved high-value) from
  soft flags (weekend expenses)
- What patterns you see (e.g. "Most hard violations are missing receipts in the Meals category")
- Immediate recommended action (e.g. "Send policy reminder to employees X, Y, Z")

---

## Critical rules
- Do NOT perform any arithmetic yourself. All amounts and counts come from the tool result.
- Do NOT invent violations not returned by the tool.
- Weekend expense flags are LOW severity — never escalate them to the same level as hard violations.
- If the tool returns zero violations, state clearly: "This batch is fully policy-compliant."
- Write for a non-technical finance audience — no jargon, no code.
- Keep the report under 500 words.
"""


def create_policy_agent() -> LlmAgent:
    """
    Instantiate the policy_compliance_agent LlmAgent.

    The agent is created fresh per pipeline run (not cached) so that
    get_settings() picks up any environment changes between runs.
    Tool injection (policy_check_tool) ensures the LLM always calls the
    deterministic engine rather than estimating policy outcomes itself.
    """
    settings = get_settings()
    return LlmAgent(
        name="policy_compliance_agent",
        model=settings.gemini_model,
        instruction=_INSTRUCTION,
        tools=[policy_check_tool],
        description=(
            "Reviews expense batches against spend limits (with optional department multipliers), "
            "receipt requirements, approval thresholds, and weekend-expense soft flags. "
            "Produces a structured compliance report separating hard violations from informational flags."
        ),
    )
