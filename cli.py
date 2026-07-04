#!/usr/bin/env python3
"""
cli.py — ExpenseAudit AI Agent Skill
--------------------------------------
Standalone command-line tool for batch expense auditing.
Can be invoked directly or scheduled as a cron job.

Usage:
    python cli.py --input data/sample_batch.json
    python cli.py --input data/sample_batch.json --mode full
    python cli.py --input data/sample_batch.json --mode deterministic --output report.json
    python cli.py --input data/sample_batch.json --drive-export

Exit codes:
    0  -- audit complete, no violations or flags
    1  -- audit complete, violations or flags found (expected -- take action)
    2  -- runtime error (invalid input, missing API key, etc.)
"""

from __future__ import annotations

import os
import sys

# WHY: Force UTF-8 on Windows before any output.
# Python on Windows defaults to cp1252 (or the system codepage) for stdout,
# which chokes on Unicode characters in vendor names, emoji in status lines,
# and any non-ASCII characters in expense descriptions.  Wrapping stdout/stderr
# at startup is simpler than sprinkling encoding=... on every print() call.
os.environ.setdefault("PYTHONUTF8", "1")
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import argparse
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Load .env before anything else
from dotenv import load_dotenv
load_dotenv()



def setup_logging(verbose: bool) -> None:
    """Configure the root logger for the CLI run.

    In quiet mode (default), WARNING+ is shown so the console output is clean
    for human consumption.  In verbose mode, DEBUG shows every engine step,
    LLM tool call, and audit-trail write — useful when diagnosing unexpected
    results or testing new fraud patterns.
    """
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def load_batch(
    input_path: str,
) -> tuple[list[dict[str, Any]], Optional[dict[str, float]]]:
    """Load expense records from a JSON file.

    Supports two formats:
      - Raw list: ``[{...}, ...]``
      - Envelope: ``{"records": [...], "department_multipliers": {...}}``

    Returns:
        Tuple of (records, department_multipliers).  department_multipliers is
        None when not present in the file.
    """
    path = Path(input_path)
    if not path.exists():
        print(f"[ERROR] Input file not found: {input_path}", file=sys.stderr)
        sys.exit(2)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Raw list — no department_multipliers
        if isinstance(data, list):
            return data, None
        if isinstance(data, dict) and "records" in data:
            multipliers = data.get("department_multipliers") or None
            return data["records"], multipliers
        print("[ERROR] Input JSON must be a list of records or {\"records\": [...]}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as exc:
        print(f"[ERROR] Invalid JSON in input file: {exc}", file=sys.stderr)
        sys.exit(2)


def print_banner() -> None:
    print("\n" + "=" * 60)
    print("  ExpenseAudit AI - Multi-Agent Expense Report Auditor")
    print("  Powered by Google ADK + Gemini 2.0 Flash")
    print("=" * 60 + "\n")


def print_deterministic_summary(
    policy_result: dict[str, Any],
    fraud_result: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    total = summary.get("total_records", 0)
    total_amount = summary.get("total_amount", 0)
    flagged = summary.get("flagged_amount", 0)
    policy_count = summary.get("policy_violations", 0)
    fraud_count = summary.get("fraud_flags", 0)
    risk = summary.get("risk_level", "UNKNOWN")
    sep = "-" * 60
    sep2 = "-" * 56

    print(sep)
    print("  AUDIT SUMMARY")
    print(sep)
    print(f"  Records reviewed    : {total}")
    print(f"  Total batch amount  : ${total_amount:,.2f}")
    print(f"  Amount flagged      : ${flagged:,.2f}")
    print(f"  Policy violations   : {policy_count}")
    print(f"  Fraud flags         : {fraud_count}")
    print(f"  Risk level          : {risk}")
    print(sep + "\n")

    if policy_count:
        print("  POLICY VIOLATIONS")
        print("  " + sep2)
        for v in policy_result.get("violations", []):
            print(f"  [{v['expense_id']}] {v['violation_type']} -- {v['detail']}")
        print()

    if fraud_count:
        print("  FRAUD FLAGS (ranked by risk)")
        print("  " + sep2)
        flags = sorted(
            fraud_result.get("flags", []),
            key=lambda f: f.get("risk_score", 0),
            reverse=True,
        )
        for f in flags:
            ids = ", ".join(f.get("expense_ids", []))
            print(
                f"  Risk {f['risk_score']}/10 | {f['flag_type']} | {ids}\n"
                f"    {f['detail']}\n"
            )

    if summary.get("recommended_actions"):
        print("  RECOMMENDED ACTIONS")
        print("  " + sep2)
        for i, action in enumerate(summary["recommended_actions"], 1):
            print(f"  {i}. {action}")
        print()


async def run_deterministic(
    batch_id: str,
    records: list[dict[str, Any]],
    department_multipliers: Optional[dict[str, float]] = None,
) -> tuple[dict, dict, dict]:
    """
    Run the deterministic audit engines and return raw result dicts.

    WHY async even though the engines are synchronous?
    --------------------------------------------------
    The CLI uses asyncio.run(main_async(...)) as its entry point because the
    full-mode pipeline (run_full) is inherently async (ADK runner uses async
    generators).  To keep a single async main_async() that handles both modes,
    deterministic mode is also wrapped in an async function so it can be
    awaited uniformly.  There is no actual I/O here — no await points — so
    this does not add latency.
    """
    from expense_audit.engine.policy_engine import run_policy_check
    from expense_audit.engine.fraud_engine import run_fraud_scan
    from expense_audit.agents.tools import run_summary_build

    policy_result = run_policy_check(records, department_multipliers=department_multipliers)
    fraud_result = run_fraud_scan(records)
    summary = run_summary_build(
        batch_id=batch_id,
        policy_result=policy_result,
        fraud_result=fraud_result,
        batch=records,
    )
    return policy_result, fraud_result, summary


async def run_full(
    batch_id: str,
    records: list[dict[str, Any]],
    submitted_by: str,
) -> dict[str, Any]:
    """
    Run the full multi-agent LLM pipeline.

    Performs an early settings check so that the error message is shown
    before any ADK imports, which can be slow (they trigger lazy Gemini
    client initialisation).  Exit code 2 (runtime error) is used rather than
    1 (findings) so that CI pipelines can distinguish a misconfigured
    environment from a legitimate audit finding.
    """
    from expense_audit.config import get_settings
    settings = get_settings()
    if not settings.llm_enabled:
        print(
            "[ERROR] GOOGLE_API_KEY is not set. Full mode requires a Gemini API key.\n"
            "        Set it in your .env file or use --mode deterministic.",
            file=sys.stderr,
        )
        sys.exit(2)

    from expense_audit.agents.orchestrator import run_pipeline
    return await run_pipeline(batch_id=batch_id, batch=records, submitted_by=submitted_by)


async def maybe_export_to_drive(report_json: str, filename: str) -> None:
    from expense_audit.mcp.drive_export import export_report_to_drive, get_drive_toolset
    toolset = await get_drive_toolset()
    result = await export_report_to_drive(report_json, filename, toolset)
    if result["success"]:
        print(f"\n  ✅ Report exported to Google Drive: {filename}")
        print(f"     File ID: {result['file_id']}")
    else:
        print(f"\n  ⚠️  Drive export skipped: {result['message']}")


async def main_async(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    print_banner()

    records, department_multipliers = load_batch(args.input)
    batch_id = args.batch_id or f"BATCH-{uuid.uuid4().hex[:8].upper()}"
    submitted_by = "cli"

    print(f"  Batch ID   : {batch_id}")
    print(f"  Records    : {len(records)}")
    print(f"  Mode       : {args.mode}")
    if department_multipliers:
        print(f"  Dept mults : {department_multipliers}")
    print(f"  Input file : {args.input}\n")

    if args.mode == "deterministic":
        print("  Running deterministic audit engines...")
        policy_result, fraud_result, summary = await run_deterministic(
            batch_id, records, department_multipliers=department_multipliers
        )
        print_deterministic_summary(policy_result, fraud_result, summary)

        output_data = {
            "batch_id": batch_id,
            "mode": "deterministic",
            "audit_timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "policy_result": policy_result,
            "fraud_result": fraud_result,
            "summary": summary,
        }

    else:  # full
        print("  Running full multi-agent pipeline (this may take ~30-60 seconds)...\n")
        result = await run_full(batch_id, records, submitted_by)

        policy_result = result.get("policy_result", {})
        fraud_result = result.get("fraud_result", {})
        summary = result.get("summary", {})

        print_deterministic_summary(policy_result, fraud_result, summary)

        if result.get("llm_policy_narrative"):
            print("  POLICY COMPLIANCE NARRATIVE (LLM)")
            print(f"  {'─' * 56}")
            print(result["llm_policy_narrative"])
            print()

        if result.get("llm_fraud_narrative"):
            print("  FRAUD ANALYSIS NARRATIVE (LLM)")
            print(f"  {'─' * 56}")
            print(result["llm_fraud_narrative"])
            print()

        if result.get("llm_executive_summary"):
            print("  EXECUTIVE SUMMARY (LLM)")
            print(f"  {'─' * 56}")
            print(result["llm_executive_summary"])
            print()

        output_data = {
            "batch_id": batch_id,
            "mode": "full",
            "audit_timestamp": datetime.now(tz=timezone.utc).isoformat(),
            **result,
        }

    # Log to audit trail
    from expense_audit.security.audit_trail import log_batch_audit
    log_batch_audit(
        batch_id=batch_id,
        submitted_by=submitted_by,
        record_count=len(records),
        policy_violations=policy_result.get("total_flagged", 0),
        fraud_flags=fraud_result.get("total_flagged", 0),
        mode=args.mode,
    )

    # Write output file if requested
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(
            json.dumps(output_data, indent=2, default=str), encoding="utf-8"
        )
        print(f"  📄 Report saved: {output_path.resolve()}\n")

    # Drive export if requested
    if args.drive_export:
        report_json = json.dumps(output_data, indent=2, default=str)
        filename = f"ExpenseAudit_{batch_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        await maybe_export_to_drive(report_json, filename)

    # WHY exit code 1 for findings (not 0):
    # The CLI is designed to be used as an "agent skill" in a scheduled pipeline.
    # Exit code 1 means "audit complete WITH findings — human action required".
    # This allows CI systems (GitHub Actions, cron wrappers) to gate on the
    # exit code: 0 = clean batch (no action needed), 1 = violations found
    # (alert finance team), 2 = runtime error (alert DevOps).
    has_findings = (
        policy_result.get("total_flagged", 0) > 0
        or fraud_result.get("total_flagged", 0) > 0
    )
    return 1 if has_findings else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="expense-audit",
        description="ExpenseAudit AI — multi-agent expense report auditor",
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to expense batch JSON file (list of records or {records:[...]})",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["deterministic", "full"],
        default="deterministic",
        help="'deterministic' uses rule engines only (fast, no API key). "
             "'full' runs the complete multi-agent LLM pipeline.",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Optional path to write the audit report JSON",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help="Optional batch ID (auto-generated if not provided)",
    )
    parser.add_argument(
        "--drive-export",
        action="store_true",
        help="Upload the report to Google Drive via MCP (requires GOOGLE_DRIVE_MCP_CREDENTIALS)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose debug logging",
    )

    args = parser.parse_args()
    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
