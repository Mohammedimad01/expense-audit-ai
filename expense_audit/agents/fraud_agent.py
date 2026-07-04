"""
expense_audit/agents/fraud_agent.py
-------------------------------------
Fraud-Pattern Detection Agent (LlmAgent).

Responsibility: Call run_fraud_scan() and produce a risk-ranked fraud analysis
narrative — explaining each pattern type, the financial exposure, and the
investigation steps a compliance team should take.

WHY dedicated fraud agent vs. combined with policy?
----------------------------------------------------
Fraud detection and policy compliance are distinct activities requiring
different expertise and output formats:
  - Policy compliance is binary: a rule is either violated or not, and
    resolution is procedural (reimburse receipt, get manager approval).
  - Fraud detection is probabilistic: risk scores, pattern combinations, and
    investigation decisions require a different mental model and different
    recommended actions (escalate to Legal, freeze card, audit history).

Separating into two LlmAgents (in a SequentialAgent) means each agent can be
given a tightly-scoped persona and instruction set, producing a more accurate
and actionable output than a single generalist agent would.

WHY is the fraud risk score computed in Python, not by the LLM?
---------------------------------------------------------------
Risk scores must be reproducible for audit purposes (same batch must always
produce the same score).  LLM outputs are non-deterministic.  The Python
detectors produce deterministic risk_score values that the LLM reads from the
tool result and includes in its narrative verbatim.
"""

from google.adk.agents import LlmAgent

from expense_audit.agents.tools import fraud_scan_tool
from expense_audit.config import get_settings

_INSTRUCTION = """
You are a Corporate Fraud Detection Specialist with expertise in expense-report
fraud patterns and financial compliance investigations.

Your job is to analyse a batch of employee expense records for fraud signals.

## Your workflow (follow exactly)
1. The batch of expense records will be provided in the session state as JSON.
2. Call `run_fraud_scan` with the full `batch` list from session state.
3. Receive the structured FraudResult back from the tool.
4. Produce a clear, risk-ranked fraud analysis with these sections:

---

### Fraud Pattern Analysis Report

**Scan Summary**
- Total records scanned: (use total_records from tool result)
- Fraud flags raised: (use total_flagged from tool result)
- Total at-risk amount: (use total_at_risk_amount from tool result)

**Flags — Ranked by Risk**
For each flag in the tool result, write:
```
Risk [risk_score]/10 | [flag_type]
   Employee: [employee_id]
   Expenses: [expense_ids]
   Amount at risk: $[total_amount]
   Finding: [detail]
   Recommended action: [one sentence]
```

Order flags from highest to lowest risk_score.

**Pattern flag types and what they mean:**
- `duplicate_submission` — same expense submitted twice; potential double-billing (risk 9)
- `vendor_anomaly` — vendor name matches shell-company or cash-out patterns (risk 8)
- `split_transaction` — same employee splits a single large expense into multiple
  same-day, same-category pieces each below the approval threshold, but their combined
  total meets or exceeds it; deliberate fragmentation to dodge manager pre-approval (risk 8)
- `threshold_skirting` — amounts in the $450–$499.99 band just below the $500 pre-approval
  threshold; deliberate avoidance of oversight (risk 4–7)
- `statistical_outlier` — an expense whose amount is a statistical outlier (z-score > 2.5)
  against that same employee's own spending history; computed with leave-one-out baseline
  so the outlier cannot mask itself by inflating the mean (risk 6)
- `round_number_padding` — statistically unusual frequency of exact dollar amounts; may
  indicate estimated or fabricated expenses (risk 5)

**Fraud Risk Assessment**
Write 2-4 sentences for a Finance Director:
- The highest-priority concern and why it warrants immediate attention
- Whether patterns appear isolated or coordinated across employees
- Any patterns that suggest intentional policy circumvention (split transactions,
  threshold skirting) vs opportunistic errors (round numbers, vendor anomaly)

---

## Critical rules
- Do NOT compute or re-score risk yourself. Use the risk_score values from the tool.
- Do NOT invent flags not returned by the tool.
- If the tool returns zero flags, state clearly: "No fraud patterns detected in this batch."
- Write for a Finance Director and Legal/Compliance team — decisive, factual language.
- Keep the report under 500 words.
"""


def create_fraud_agent() -> LlmAgent:
    """
    Instantiate the fraud_pattern_agent LlmAgent.

    Only fraud_scan_tool is injected — not the policy tool.  This scopes the
    agent to a single responsibility and prevents it from accidentally calling
    the wrong tool and producing confused output.
    """
    settings = get_settings()
    return LlmAgent(
        name="fraud_pattern_agent",
        model=settings.gemini_model,
        instruction=_INSTRUCTION,
        tools=[fraud_scan_tool],
        description=(
            "Detects duplicate submissions, round-number padding, threshold-skirting, "
            "vendor anomalies, split transactions, and statistical outliers. "
            "Produces a risk-ranked fraud analysis."
        ),
    )
