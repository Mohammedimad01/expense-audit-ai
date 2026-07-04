"""
expense_audit/engine/policy_engine.py
--------------------------------------
Deterministic policy-compliance rule engine.
NO LLM is used here — all decisions are pure Python arithmetic and conditionals
so they are reproducible, auditable, and independently testable.

Rules enforced:
  1. Category spend limit exceeded (with optional per-department multipliers)
  2. Missing receipt
  3. Expense >= $500 without manager pre-approval
  4. Weekend expense (LOW severity — informational flag, not a hard breach)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from expense_audit.config import get_settings
from expense_audit.models import PolicyResult, PolicyViolation, PolicyViolationType

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Individual rule checkers
# ──────────────────────────────────────────────────────────────────────────────

def check_category_limit(
    record: dict[str, Any],
    department_multipliers: Optional[dict[str, float]] = None,
) -> PolicyViolation | None:
    """Flag a record whose amount exceeds the per-category spend cap.

    Args:
        record: Expense record dict.
        department_multipliers: Optional mapping of department name -> float
            multiplier applied to the base category limit.  Departments absent
            from the dict default to 1.0x (no adjustment).
    """
    settings = get_settings()
    category = record.get("category", "")
    amount = float(record.get("amount", 0))
    base_limit = settings.category_limits.get(category)

    if base_limit is None:
        # Unknown category — cannot enforce limit
        return None

    # Apply department multiplier (default 1.0 for unlisted departments)
    department = record.get("department", "")
    multiplier = 1.0
    if department_multipliers:
        multiplier = department_multipliers.get(department, 1.0)

    effective_limit = base_limit * multiplier

    if amount > effective_limit:
        if multiplier != 1.0:
            detail = (
                f"Amount ${amount:.2f} exceeds the {category} limit of "
                f"${effective_limit:.2f} (${base_limit:.2f} base × {multiplier}x "
                f"{department} multiplier; overage: ${amount - effective_limit:.2f})"
            )
        else:
            detail = (
                f"Amount ${amount:.2f} exceeds the {category} limit of "
                f"${effective_limit:.2f} (overage: ${amount - effective_limit:.2f})"
            )
        return PolicyViolation(
            expense_id=record["expense_id"],
            employee_id=record["employee_id"],
            violation_type=PolicyViolationType.CATEGORY_LIMIT_EXCEEDED,
            detail=detail,
            amount=amount,
            category=category,
        )
    return None


def check_missing_receipt(record: dict[str, Any]) -> PolicyViolation | None:
    """Flag a record without an attached receipt."""
    if not record.get("has_receipt", True):
        amount = float(record.get("amount", 0))
        return PolicyViolation(
            expense_id=record["expense_id"],
            employee_id=record["employee_id"],
            violation_type=PolicyViolationType.MISSING_RECEIPT,
            detail=(
                f"No receipt attached for ${amount:.2f} {record.get('category', 'Unknown')} "
                f"expense at {record.get('vendor', 'unknown vendor')}"
            ),
            amount=amount,
            category=record.get("category", "Unknown"),
        )
    return None


def check_approval_threshold(record: dict[str, Any]) -> PolicyViolation | None:
    """Flag a record at or above the approval threshold without manager sign-off."""
    settings = get_settings()
    amount = float(record.get("amount", 0))
    manager_approved = record.get("manager_approved", False)

    if amount >= settings.approval_threshold and not manager_approved:
        return PolicyViolation(
            expense_id=record["expense_id"],
            employee_id=record["employee_id"],
            violation_type=PolicyViolationType.APPROVAL_THRESHOLD,
            detail=(
                f"Amount ${amount:.2f} meets or exceeds the ${settings.approval_threshold:.2f} "
                f"pre-approval threshold but no manager approval on record"
            ),
            amount=amount,
            category=record.get("category", "Unknown"),
        )
    return None


def check_weekend_expense(record: dict[str, Any]) -> PolicyViolation | None:
    """Flag expenses incurred on Saturday or Sunday (LOW severity — informational).

    This is not a hard policy breach; it simply draws reviewer attention to
    weekend spending that may indicate personal-use misclassification.
    Malformed dates are silently skipped — this check must never crash the audit.
    """
    raw_date = record.get("expense_date", "")
    try:
        expense_dt = datetime.strptime(str(raw_date), "%Y-%m-%d")
    except (ValueError, TypeError):
        # Gracefully skip malformed or missing dates
        return None

    weekday = expense_dt.weekday()  # 0=Mon … 5=Sat, 6=Sun
    if weekday not in (5, 6):
        return None

    day_name = "Saturday" if weekday == 5 else "Sunday"
    amount = float(record.get("amount", 0))
    return PolicyViolation(
        expense_id=record["expense_id"],
        employee_id=record["employee_id"],
        violation_type=PolicyViolationType.WEEKEND_EXPENSE,
        detail=(
            f"Expense of ${amount:.2f} for {record.get('category', 'Unknown')} "
            f"was incurred on a {day_name} ({raw_date}) — "
            f"flagged for reviewer attention (LOW severity)"
        ),
        amount=amount,
        category=record.get("category", "Unknown"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public API — called by FunctionTool wrapper
# ──────────────────────────────────────────────────────────────────────────────

def run_policy_check(
    batch: list[dict[str, Any]],
    department_multipliers: Optional[dict[str, float]] = None,
) -> dict[str, Any]:
    """
    Run all policy rules against a batch of expense records.

    Args:
        batch: List of expense record dicts (matching ExpenseRecord schema).
        department_multipliers: Optional mapping of department name -> float
            multiplier for per-department category-limit adjustments.
            E.g. {"Sales": 1.5, "Marketing": 1.2}.  Absent departments use 1.0x.

    Returns:
        PolicyResult serialised as a dict.
    """
    logger.info("Running policy check on %d records", len(batch))
    violations: list[PolicyViolation] = []

    for record in batch:
        # Rules that take department_multipliers
        for rule_fn, kwargs in [
            (check_category_limit, {"department_multipliers": department_multipliers}),
            (check_missing_receipt, {}),
            (check_approval_threshold, {}),
            (check_weekend_expense, {}),
        ]:
            try:
                violation = rule_fn(record, **kwargs)
                if violation:
                    violations.append(violation)
            except Exception as exc:
                logger.warning(
                    "Policy rule %s failed on expense_id=%s: %s",
                    rule_fn.__name__,
                    record.get("expense_id", "?"),
                    exc,
                )

    result = PolicyResult(
        total_records=len(batch),
        violations=violations,
    )
    logger.info(
        "Policy check complete: %d violations found across %d records",
        result.total_flagged,
        result.total_records,
    )
    return result.model_dump(mode="json")
