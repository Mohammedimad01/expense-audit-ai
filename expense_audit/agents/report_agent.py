"""
expense_audit/agents/report_agent.py
--------------------------------------
Summary & Report Agent (LlmAgent).

Responsibility: Call run_summary_build() then synthesise the policy compliance
report and fraud analysis into a concise executive summary a Finance Director
can read and act on in under 5 minutes.

Drive export (opt-in):
  Set GOOGLE_DRIVE_MCP_CREDENTIALS in your environment and pass
  enable_drive_export=True to create_report_agent() to attach the Drive
  MCPToolset.  The agent will then call the upload_report_to_drive tool at
  the end of its run.

  If GOOGLE_DRIVE_MCP_CREDENTIALS is not set, Drive export is silently
  disabled regardless of the flag value.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.agents import LlmAgent

from expense_audit.agents.tools import summary_build_tool
from expense_audit.config import get_settings

logger = logging.getLogger(__name__)

_INSTRUCTION = """
You are a Financial Reporting Analyst who translates audit findings into executive-level summaries.

Your job is to combine the outputs of the Policy Compliance Report and Fraud Pattern Analysis
into a final, prioritised executive summary.

## Your workflow (follow exactly)
1. The session state will contain:
   - `batch_id`: the batch identifier
   - `policy_result`: the structured output from the policy compliance check
   - `fraud_result`: the structured output from the fraud scan
   - `batch`: the original expense records
2. Call `run_summary_build` with all four arguments.
3. Receive the structured `SummaryResult` back from the tool.
4. Produce the final executive report:

---

# ExpenseAudit AI — Executive Summary
**Batch:** {batch_id} | **Risk Level:** {risk_level} | **Generated:** [current date]

## At a Glance
| Metric | Value |
|---|---|
| Records Reviewed | {total_records} |
| Total Batch Amount | ${total_amount} |
| Amount Flagged | ${flagged_amount} |
| Policy Violations | {policy_violations} |
| Fraud Flags | {fraud_flags} |
| Overall Risk | **{risk_level}** |

## Key Findings
[List each item in top_issues as a numbered finding with one sentence of context]

## Recommended Actions
[List each recommended_action as a numbered action item with owner and urgency]

## Next Steps
Write 2–3 sentences on what the Finance Director should do today vs. this week.

---

## Critical rules
- Do NOT perform any calculations. All numbers come from the tool result.
- Do NOT reference the policy or fraud reports' internal details — summarise only.
- If risk_level is LOW and both violations and flags are 0: write a brief clean-bill-of-health.
- Language must be suitable for a non-technical Finance Director.
- Keep the executive summary under 300 words (excluding the table).
"""

_INSTRUCTION_WITH_DRIVE = _INSTRUCTION + """

## Drive export (if upload_report_to_drive tool is available)
5. After producing the executive report, call `export_report_to_drive` with:
   - `report_json`: a JSON-serialised version of the SummaryResult dict
   - `filename`: f"audit_report_{batch_id}.json"
   Acknowledge the upload result in one final sentence.
"""


def create_report_agent(enable_drive_export: bool = False) -> LlmAgent:
    """
    Create the summary_report_agent LlmAgent.

    Args:
        enable_drive_export: When True, attempt to attach the Google Drive
            MCPToolset so the agent can upload the final report to Drive.
            Requires GOOGLE_DRIVE_MCP_CREDENTIALS to be set in the environment.
            If the toolset cannot be initialised, the agent falls back to
            running without Drive export (no crash).

    Returns:
        A configured LlmAgent instance.
    """
    settings = get_settings()
    tools: list[Any] = [summary_build_tool]
    instruction = _INSTRUCTION

    if enable_drive_export and settings.drive_mcp_enabled:
        # Lazy import to avoid loading MCP machinery when Drive is not needed
        try:
            from expense_audit.mcp.drive_export import get_drive_toolset  # noqa: PLC0415
            import asyncio  # noqa: PLC0415

            # get_drive_toolset is async — run it synchronously at agent-creation time.
            # This is acceptable because agent creation is a one-time setup step.
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            drive_toolset = loop.run_until_complete(get_drive_toolset())
            if drive_toolset is not None:
                tools.append(drive_toolset)
                instruction = _INSTRUCTION_WITH_DRIVE
                logger.info(
                    "Google Drive MCPToolset attached to summary_report_agent"
                )
            else:
                logger.warning(
                    "enable_drive_export=True but Drive toolset could not be "
                    "initialised — running without Drive export"
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to attach Drive MCPToolset: %s — continuing without Drive export",
                exc,
            )
    elif enable_drive_export and not settings.drive_mcp_enabled:
        logger.warning(
            "enable_drive_export=True but GOOGLE_DRIVE_MCP_CREDENTIALS is not set — "
            "Drive export skipped.  "
            "Set GOOGLE_DRIVE_MCP_CREDENTIALS to a service-account JSON path to enable it."
        )

    return LlmAgent(
        name="summary_report_agent",
        model=settings.gemini_model,
        instruction=instruction,
        tools=tools,
        description=(
            "Aggregates policy and fraud findings into a structured executive summary "
            "with risk levels, top issues, and recommended actions."
        ),
    )
