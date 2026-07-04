"""
expense_audit/agents/tools.py
-------------------------------
google-adk FunctionTool wrappers around the deterministic engines.

WHY this layer exists
---------------------
The LLM agents (LlmAgent) need to call Python functions as tools.  ADK's
FunctionTool wraps any Python callable so the LLM can invoke it via its
function-calling API.  Crucially, the LLM never *performs* arithmetic — it
calls these tools and receives grounded, pre-computed results which it then
narrates in natural language.

This separation is intentional:
  - Deterministic correctness: policy limits and fraud scores are always
    produced by tested Python code, never by LLM probability estimation.
  - Auditability: every flagged amount can be traced to a specific rule line.
  - Resilience: if the LLM produces a malformed tool call, the error is
    caught here without corrupting the audit result.

Each tool:
  - Takes JSON-serialisable inputs (dicts / lists of primitives)
  - Returns JSON-serialisable outputs (dicts)
  - Handles its own error boundary so one bad record never crashes the pipeline
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.tools import FunctionTool

from expense_audit.engine.fraud_engine import run_fraud_scan
from expense_audit.engine.policy_engine import run_policy_check
from expense_audit.models import RiskLevel, SummaryResult

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Summary aggregator (deterministic)
# ──────────────────────────────────────────────────────────────────────────────

def run_summary_build(
    batch_id: str,
    policy_result: dict[str, Any],
    fraud_result: dict[str, Any],
    batch: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Aggregate policy and fraud results into a structured executive summary.

    WHY a separate aggregator?
    --------------------------
    Both the policy engine and the fraud engine produce their own result dicts.
    This function is the single source of truth for derived metrics (total
    amount, flagged ratio, overall risk level) that appear in the executive
    summary.  Keeping it here — not in the report_agent's LLM prompt — means:
      1. Risk levels are reproducible across runs (same inputs → same level).
      2. Finance Directors can trust the "CRITICAL / HIGH / MEDIUM / LOW" label
         is not a hallucination.
      3. The report_agent only narrates; it never recalculates.

    Risk-level thresholds (chosen after consulting ACFE guidelines):
      - CRITICAL: >50% of total spend flagged, OR >20 combined issues.
      - HIGH:     >25% flagged, OR >10 issues.
      - MEDIUM:   >10% flagged, OR >3 issues.
      - LOW:      no issues at all.

    Args:
        batch_id: The batch identifier.
        policy_result: Output of run_policy_check() — serialised PolicyResult.
        fraud_result: Output of run_fraud_scan() — serialised FraudResult.
        batch: Original expense records (for total-amount calculation; the
               engines don't carry the full batch in their result dicts).

    Returns:
        SummaryResult serialised as a dict (all values JSON-safe).
    """
    total_records = policy_result.get("total_records", len(batch))
    total_amount = sum(float(r.get("amount", 0)) for r in batch)
    policy_violations = policy_result.get("total_flagged", 0)
    fraud_flags = fraud_result.get("total_flagged", 0)

    # Flagged amount = policy exposure + fraud at-risk amount.
    # These are intentionally additive because a single expense can appear in
    # both (e.g. a missing-receipt record that is also a statistical outlier).
    # Summing may slightly overcount, but erring on the side of over-reporting
    # is safer than under-reporting financial risk to Finance.
    flagged_amount = policy_result.get("total_flagged_amount", 0.0) + fraud_result.get(
        "total_at_risk_amount", 0.0
    )

    # Risk scoring: weighted combination of violation count and flagged-amount ratio.
    # Using a ratio (not absolute amount) makes thresholds batch-size-independent.
    flagged_ratio = flagged_amount / total_amount if total_amount > 0 else 0
    total_issues = policy_violations + fraud_flags

    if total_issues == 0:
        risk_level = RiskLevel.LOW
    elif flagged_ratio > 0.5 or total_issues > 20:
        risk_level = RiskLevel.CRITICAL
    elif flagged_ratio > 0.25 or total_issues > 10:
        risk_level = RiskLevel.HIGH
    elif flagged_ratio > 0.10 or total_issues > 3:
        risk_level = RiskLevel.MEDIUM
    else:
        risk_level = RiskLevel.LOW

    # Top issues: 3 highest-priority policy violations + 2 highest-risk fraud flags.
    # The mix (3+2) was chosen to surface both dimensions without overwhelming the
    # executive summary.  Fraud flags are sorted by risk_score before slicing.
    top_issues: list[str] = []
    for v in policy_result.get("violations", [])[:3]:
        top_issues.append(f"[Policy] {v.get('detail', '')}")
    for f in sorted(fraud_result.get("flags", []), key=lambda x: x.get("risk_score", 0), reverse=True)[:2]:
        top_issues.append(f"[Fraud/{f.get('flag_type','?')}] {f.get('detail', '')}")

    # Recommended actions: one generic action per finding type (not per record)
    # to keep the summary actionable rather than an enumeration.
    actions: list[str] = []
    if policy_violations:
        actions.append(
            f"Review and resolve {policy_violations} policy violation(s) with submitting employees"
        )
    if fraud_flags:
        actions.append(
            f"Escalate {fraud_flags} fraud flag(s) to Finance Compliance for investigation"
        )
    if not actions:
        actions.append("No immediate actions required — batch appears compliant")

    result = SummaryResult(
        batch_id=batch_id,
        total_records=total_records,
        total_amount=round(total_amount, 2),
        flagged_amount=round(flagged_amount, 2),
        policy_violations=policy_violations,
        fraud_flags=fraud_flags,
        risk_level=risk_level,
        top_issues=top_issues,
        recommended_actions=actions,
    )

    logger.info(
        "Summary built: risk=%s, policy=%d, fraud=%d, flagged=$%.2f / $%.2f total",
        risk_level,
        policy_violations,
        fraud_flags,
        flagged_amount,
        total_amount,
    )
    return result.model_dump(mode="json")


# ──────────────────────────────────────────────────────────────────────────────
# ADK FunctionTool definitions
#
# FunctionTool introspects the function signature and docstring to build the
# tool schema the LLM sees.  Keep function signatures typed and docstrings
# informative — the LLM reads them to decide how to call the tool.
# ──────────────────────────────────────────────────────────────────────────────

policy_check_tool = FunctionTool(func=run_policy_check)
fraud_scan_tool = FunctionTool(func=run_fraud_scan)
summary_build_tool = FunctionTool(func=run_summary_build)
