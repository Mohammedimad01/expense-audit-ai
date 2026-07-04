"""
expense_audit/agents/orchestrator.py
--------------------------------------
Orchestrator — SequentialAgent that runs the full audit pipeline.

Pipeline order:
  1. policy_compliance_agent → checks spend limits (with dept multipliers),
     receipts, approvals, and weekend flags
  2. fraud_pattern_agent     → detects duplicates, padding, skirting,
     vendor anomalies, split transactions, statistical outliers
  3. summary_report_agent    → aggregates both into an executive report

Design principle: deterministic engines run FIRST and inject grounded results
into session state.  The LLM agents narrate and explain those pre-computed
results — they never re-derive dollar amounts or counts themselves.

Session state is passed between agents automatically by the SequentialAgent.
Each LlmAgent reads from and writes to the shared `session.state` dict.

Usage:
    from expense_audit.agents.orchestrator import run_pipeline
    result = await run_pipeline(
        batch_id="B001",
        batch=[...],
        department_multipliers={"Sales": 1.5},
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from google.adk.agents import SequentialAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from expense_audit.agents.fraud_agent import create_fraud_agent
from expense_audit.agents.policy_agent import create_policy_agent
from expense_audit.agents.report_agent import create_report_agent
from expense_audit.config import get_settings

logger = logging.getLogger(__name__)

APP_NAME = "expense_audit_ai"


def create_pipeline(enable_drive_export: bool = False) -> SequentialAgent:
    """Build and return the full audit SequentialAgent.

    Args:
        enable_drive_export: When True, the report agent will attempt to attach
            the Google Drive MCPToolset (requires GOOGLE_DRIVE_MCP_CREDENTIALS).
    """
    return SequentialAgent(
        name="expense_audit_orchestrator",
        description=(
            "Orchestrates the full expense-audit pipeline: "
            "policy compliance check (spend limits, receipts, approvals, weekend flags) "
            "→ fraud pattern scan (duplicates, padding, skirting, split transactions, outliers) "
            "→ executive summary generation"
        ),
        sub_agents=[
            create_policy_agent(),
            create_fraud_agent(),
            create_report_agent(enable_drive_export=enable_drive_export),
        ],
    )


async def run_pipeline(
    batch_id: str,
    batch: list[dict[str, Any]],
    submitted_by: str = "system",
    department_multipliers: Optional[dict[str, float]] = None,
    enable_drive_export: bool = False,
) -> dict[str, Any]:
    """
    Run the full multi-agent audit pipeline on a batch of expense records.

    The deterministic engines (policy + fraud) are run first and their results
    are injected into the session state so LLM agents have grounded, pre-computed
    numbers to narrate rather than rediscovering them.

    Args:
        batch_id: Unique identifier for this batch.
        batch: List of expense record dicts.
        submitted_by: User / system initiating the audit.
        department_multipliers: Optional per-department category-limit multipliers
            (e.g. {"Sales": 1.5}).  Passed to run_policy_check.
        enable_drive_export: When True, the report agent will attempt to upload
            the final report to Google Drive via MCPToolset.  Requires
            GOOGLE_DRIVE_MCP_CREDENTIALS to be set in the environment.

    Returns:
        Dict with keys: policy_result, fraud_result, summary,
        llm_policy_narrative, llm_fraud_narrative, llm_executive_summary
    """
    settings = get_settings()
    if not settings.llm_enabled:
        raise EnvironmentError(
            "GOOGLE_API_KEY is not set. "
            "The full LLM pipeline requires a Gemini API key. "
            "Use /audit/deterministic for a no-LLM audit."
        )

    # ── Pre-run deterministic engines ────────────────────────────────────────
    # Grounding strategy: compute all numbers before the LLM agents run.
    # This prevents the LLM from hallucinating amounts or re-deriving counts.
    from expense_audit.engine.policy_engine import run_policy_check
    from expense_audit.engine.fraud_engine import run_fraud_scan
    from expense_audit.agents.tools import run_summary_build

    logger.info(
        "Pre-running deterministic engines for batch %s (%d records)", batch_id, len(batch)
    )
    policy_result = run_policy_check(batch, department_multipliers=department_multipliers)
    fraud_result = run_fraud_scan(batch)
    summary = run_summary_build(
        batch_id=batch_id,
        policy_result=policy_result,
        fraud_result=fraud_result,
        batch=batch,
    )

    # ── Build pipeline and session service ───────────────────────────────────────
    pipeline = create_pipeline(enable_drive_export=enable_drive_export)
    session_service = InMemorySessionService()

    # Initial state passed to all agents via session.
    # Pre-populated with grounded deterministic results.
    # Flat scalars (batch_id, risk_level, etc.) are also injected so the
    # report_agent's instruction template can be filled by ADK's inject_session_state.
    initial_state: dict[str, Any] = {
        "batch_id": batch_id,
        "batch": batch,
        "submitted_by": submitted_by,
        "department_multipliers": department_multipliers or {},
        # Grounded results available to all agents immediately
        "policy_result": policy_result,
        "fraud_result": fraud_result,
        "summary": summary,
        # Flat scalars for report_agent instruction template substitution
        "risk_level": str(summary.get("risk_level", "UNKNOWN")),
        "total_records": summary.get("total_records", len(batch)),
        "total_amount": summary.get("total_amount", 0.0),
        "flagged_amount": summary.get("flagged_amount", 0.0),
        "policy_violations": summary.get("policy_violations", 0),
        "fraud_flags": summary.get("fraud_flags", 0),
    }

    # Create a session with the initial state
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=submitted_by,
        session_id=batch_id,
        state=initial_state,
    )

    runner = Runner(
        agent=pipeline,
        app_name=APP_NAME,
        session_service=session_service,
    )

    # Compose the initial user message that kicks off the pipeline.
    # Agents are instructed to call their respective tools — those tools will
    # re-run the deterministic engines and produce the same results (idempotent),
    # or the agents may use the pre-populated state directly.
    dept_hint = (
        f" Department limit multipliers are in effect: {department_multipliers}."
        if department_multipliers
        else ""
    )
    user_message = genai_types.Content(
        role="user",
        parts=[
            genai_types.Part(
                text=(
                    f"Please audit expense batch '{batch_id}' with {len(batch)} records.{dept_hint} "
                    f"The batch data and pre-computed deterministic results are available "
                    f"in session state. Call your tools to produce the narrative reports."
                )
            )
        ],
    )

    # Collect all events from the pipeline
    policy_narrative = ""
    fraud_narrative = ""
    executive_summary = ""
    agent_responses: dict[str, str] = {}

    logger.info("Starting LLM pipeline for batch %s (%d records)", batch_id, len(batch))

    async for event in runner.run_async(
        user_id=submitted_by,
        session_id=batch_id,
        new_message=user_message,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            text = event.content.parts[0].text or ""
            agent_name = getattr(event, "author", "unknown")
            agent_responses[agent_name] = text
            logger.debug("Agent '%s' responded (%d chars)", agent_name, len(text))

    # Map agent responses to named outputs
    for agent_name, text in agent_responses.items():
        if "policy" in agent_name.lower():
            policy_narrative = text
        elif "fraud" in agent_name.lower():
            fraud_narrative = text
        elif "report" in agent_name.lower() or "summary" in agent_name.lower():
            executive_summary = text

    # Retrieve final session state
    final_session = await session_service.get_session(
        app_name=APP_NAME,
        user_id=submitted_by,
        session_id=batch_id,
    )
    final_state = final_session.state if final_session else {}

    return {
        "policy_result": final_state.get("policy_result", policy_result),
        "fraud_result": final_state.get("fraud_result", fraud_result),
        "summary": final_state.get("summary", summary),
        "llm_policy_narrative": policy_narrative,
        "llm_fraud_narrative": fraud_narrative,
        "llm_executive_summary": executive_summary,
    }
