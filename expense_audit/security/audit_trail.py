"""
expense_audit/security/audit_trail.py
----------------------------------------
Append-only audit trail for batch-level auditing metadata.

What gets logged (batch-level only, never individual expense PII):
  - batch_id
  - timestamp (ISO-8601 UTC)
  - submitted_by (the user / system that invoked the audit — not employee names)
  - record_count
  - policy_violations count
  - fraud_flags count
  - mode ("deterministic" or "full")

What is NEVER logged:
  - Individual expense amounts or descriptions
  - Employee names or raw employee IDs
  - Vendor names
  - Any field from ExpenseRecord

The trail is written to `audit_trail.jsonl` in the working directory.
Each line is a valid JSON object (JSONL format) for easy streaming reads.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_AUDIT_FILE = Path(os.environ.get("AUDIT_TRAIL_PATH", "audit_trail.jsonl"))


def log_batch_audit(
    batch_id: str,
    submitted_by: str,
    record_count: int,
    policy_violations: int,
    fraud_flags: int,
    mode: str,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """
    Append one audit entry to the trail.

    Args:
        batch_id: Unique batch identifier.
        submitted_by: Who triggered the audit (user or "system").
        record_count: Number of expense records in the batch.
        policy_violations: Number of policy violations found.
        fraud_flags: Number of fraud flags raised.
        mode: "deterministic" or "full".
        extra: Optional additional metadata (must not contain PII).
    """
    entry: dict[str, Any] = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "batch_id": batch_id,
        "submitted_by": submitted_by,
        "record_count": record_count,
        "policy_violations": policy_violations,
        "fraud_flags": fraud_flags,
        "mode": mode,
    }
    if extra:
        # Validate extra doesn't contain obvious PII keys
        pii_keys = {"employee_name", "employee_id", "vendor", "description", "amount"}
        safe_extra = {k: v for k, v in extra.items() if k not in pii_keys}
        entry.update(safe_extra)

    try:
        with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        logger.debug("Audit trail entry written: batch=%s", batch_id)
    except OSError as exc:
        # Never let audit-trail failure crash the main pipeline
        logger.error("Failed to write audit trail entry: %s", exc)


def read_audit_trail(limit: int = 100) -> list[dict[str, Any]]:
    """
    Read the most recent N entries from the audit trail.

    Args:
        limit: Maximum number of entries to return (most recent first).

    Returns:
        List of audit trail dicts.
    """
    if not _AUDIT_FILE.exists():
        return []
    try:
        lines = _AUDIT_FILE.read_text(encoding="utf-8").strip().splitlines()
        entries = [json.loads(line) for line in lines if line.strip()]
        return list(reversed(entries[-limit:]))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to read audit trail: %s", exc)
        return []
