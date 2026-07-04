"""
tests/test_policy_engine.py
-----------------------------
Unit tests for the deterministic policy compliance engine.

Test matrix:
  1. test_category_limit_exceeded — Meals $75 → flagged
  2. test_missing_receipt         — has_receipt=False → flagged
  3. test_approval_threshold      — $550 unapproved → flagged
  4. test_clean_record            — Compliant record → zero violations
"""

import pytest

from expense_audit.engine.policy_engine import (
    check_approval_threshold,
    check_category_limit,
    check_missing_receipt,
    run_policy_check,
)
from expense_audit.models import PolicyViolationType


class TestCategoryLimit:
    def test_over_limit_meals_flagged(self, over_limit_meals_record):
        """Meals expense at $75 exceeds the $50 limit — must be flagged."""
        violation = check_category_limit(over_limit_meals_record)
        assert violation is not None, "Expected a violation for over-limit Meals expense"
        assert violation.violation_type == PolicyViolationType.CATEGORY_LIMIT_EXCEEDED
        assert violation.expense_id == "EXP-LIMIT-001"
        assert violation.amount == 75.00
        assert "Meals" in violation.detail

    def test_under_limit_not_flagged(self, clean_record):
        """Meals expense at $35 is within the $50 limit — must not be flagged."""
        violation = check_category_limit(clean_record)
        assert violation is None, f"Unexpected violation for compliant record: {violation}"

    def test_unknown_category_not_flagged(self):
        """Category not in policy config — should be ignored, not flagged."""
        record = {
            "expense_id": "EXP-UNKNOWN",
            "employee_id": "EMP-X",
            "category": "Parking",  # not in LIMITS
            "amount": 9999.99,
            "has_receipt": True,
            "manager_approved": True,
        }
        violation = check_category_limit(record)
        assert violation is None


class TestMissingReceipt:
    def test_missing_receipt_flagged(self, missing_receipt_record):
        """Record with has_receipt=False must be flagged."""
        violation = check_missing_receipt(missing_receipt_record)
        assert violation is not None
        assert violation.violation_type == PolicyViolationType.MISSING_RECEIPT
        assert violation.expense_id == "EXP-NORECEIPT-001"

    def test_receipt_present_not_flagged(self, clean_record):
        """Record with has_receipt=True must not be flagged."""
        violation = check_missing_receipt(clean_record)
        assert violation is None


class TestApprovalThreshold:
    def test_unapproved_high_value_flagged(self, approval_threshold_record):
        """Expense >= $500 without manager approval must be flagged."""
        violation = check_approval_threshold(approval_threshold_record)
        assert violation is not None
        assert violation.violation_type == PolicyViolationType.APPROVAL_THRESHOLD
        assert violation.expense_id == "EXP-THRESH-001"
        assert violation.amount == 550.00

    def test_approved_high_value_not_flagged(self):
        """Expense >= $500 WITH manager approval must NOT be flagged."""
        record = {
            "expense_id": "EXP-APPROVED",
            "employee_id": "EMP-OK",
            "category": "Travel",
            "amount": 800.00,
            "has_receipt": True,
            "manager_approved": True,
        }
        violation = check_approval_threshold(record)
        assert violation is None

    def test_below_threshold_not_flagged(self, clean_record):
        """Expense well below $500 — threshold rule should not trigger."""
        violation = check_approval_threshold(clean_record)
        assert violation is None


class TestRunPolicyCheck:
    def test_clean_record_zero_violations(self, clean_record):
        """A fully compliant batch returns zero violations."""
        result = run_policy_check([clean_record])
        assert result["total_records"] == 1
        assert result["total_flagged"] == 0
        assert result["violations"] == []

    def test_multiple_violations_detected(
        self, over_limit_meals_record, missing_receipt_record, approval_threshold_record
    ):
        """All three violation types detected in a mixed batch.

        Note: approval_threshold_record ($550 Lodging) triggers BOTH category_limit_exceeded
        ($550 > $300 Lodging limit) AND approval_threshold_exceeded — so total_flagged >= 3.
        """
        batch = [over_limit_meals_record, missing_receipt_record, approval_threshold_record]
        result = run_policy_check(batch)
        assert result["total_records"] == 3
        # At least 3 violations; may be more if a record triggers multiple rules
        assert result["total_flagged"] >= 3

        violation_types = {v["violation_type"] for v in result["violations"]}
        assert "category_limit_exceeded" in violation_types
        assert "missing_receipt" in violation_types
        assert "approval_threshold_exceeded" in violation_types

    def test_flagged_amount_correct(self, over_limit_meals_record):
        """Flagged amount equals the sum of flagged record amounts."""
        result = run_policy_check([over_limit_meals_record])
        assert result["total_flagged_amount"] == pytest.approx(75.00)
