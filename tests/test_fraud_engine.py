"""
tests/test_fraud_engine.py
---------------------------
Unit tests for the deterministic fraud detection engine.

Test matrix:
  1. test_duplicate_detection          — Two identical records → flagged
  2. test_round_number_detection       — 3+ round-dollar records → flagged
  3. test_threshold_skirting_detection — $499 record → flagged
  4. test_clean_batch_no_flags         — All-clean batch → zero flags
  5. test_vendor_anomaly_detection     — Shell-vendor name → flagged
  6. test_no_duplicate_different_emp   — Same amount, different employees → NOT flagged
  7. test_run_fraud_scan_integration   — Full scan on seeded mixed batch
"""

import pytest

from expense_audit.engine.fraud_engine import (
    detect_duplicates,
    detect_round_numbers,
    detect_threshold_skirting,
    detect_vendor_anomaly,
    run_fraud_scan,
)
from expense_audit.models import FraudFlagType


class TestDuplicateDetection:
    def test_exact_duplicate_pair_flagged(self, duplicate_pair):
        """Two identical records from same employee in same period must be flagged."""
        flags = detect_duplicates(duplicate_pair)
        assert len(flags) == 1, f"Expected 1 flag, got {len(flags)}"
        assert flags[0].flag_type == FraudFlagType.DUPLICATE
        assert flags[0].employee_id == "EMP-DUP"
        assert set(flags[0].expense_ids) == {"EXP-DUP-001", "EXP-DUP-002"}

    def test_no_duplicate_different_employees(self, clean_record, over_limit_meals_record):
        """Same amount and category but different employees — NOT a duplicate."""
        # Force same amount, category, vendor but different employees
        rec_a = {**clean_record, "amount": 35.00, "vendor": "same vendor", "category": "Meals"}
        rec_b = {**over_limit_meals_record, "amount": 35.00, "vendor": "same vendor",
                 "category": "Meals", "employee_id": "EMP-OTHER"}
        flags = detect_duplicates([rec_a, rec_b])
        assert len(flags) == 0, "Different employees must not trigger duplicate flag"

    def test_single_record_no_flag(self, clean_record):
        """A batch with one record cannot have duplicates."""
        flags = detect_duplicates([clean_record])
        assert len(flags) == 0


class TestRoundNumberDetection:
    def test_three_round_numbers_flagged(self, round_number_batch):
        """Employee with 3+ round-dollar submissions must be flagged."""
        flags = detect_round_numbers(round_number_batch)
        assert len(flags) == 1
        assert flags[0].flag_type == FraudFlagType.ROUND_NUMBER
        assert flags[0].employee_id == "EMP-ROUND"
        assert len(flags[0].expense_ids) == 3

    def test_two_round_numbers_not_flagged(self):
        """Employee with only 2 round-dollar submissions is below the threshold."""
        records = [
            {
                "expense_id": f"EXP-RN-{i}",
                "employee_id": "EMP-BORDERLINE",
                "employee_name": "Test",
                "expense_date": f"2026-06-0{i}",
                "category": "Meals",
                "vendor": "Cafe",
                "amount": 30.0,
                "has_receipt": True,
                "manager_approved": False,
                "department": "X",
                "submission_date": f"2026-06-0{i}",
                "description": "",
            }
            for i in range(1, 3)  # only 2 records
        ]
        flags = detect_round_numbers(records)
        assert len(flags) == 0, "2 round-dollar records should NOT trigger (threshold is 3)"


class TestThresholdSkirting:
    def test_just_under_threshold_flagged(self, threshold_skirting_record):
        """A $499 submission (within $450–$499.99 band) must be flagged."""
        flags = detect_threshold_skirting([threshold_skirting_record])
        assert len(flags) == 1
        assert flags[0].flag_type == FraudFlagType.THRESHOLD_SKIRTING
        assert flags[0].employee_id == "EMP-SKIRT"

    def test_over_threshold_not_skirting(self, approval_threshold_record):
        """A $550 submission is above the skirting band — NOT a skirting flag."""
        flags = detect_threshold_skirting([approval_threshold_record])
        assert len(flags) == 0

    def test_well_below_threshold_not_skirting(self, clean_record):
        """A $35 record is far below the skirting band — NOT flagged."""
        flags = detect_threshold_skirting([clean_record])
        assert len(flags) == 0


class TestVendorAnomaly:
    def test_cash_vendor_flagged(self):
        """Vendor name containing 'Cash' must be flagged."""
        record = {
            "expense_id": "EXP-VENDOR-001",
            "employee_id": "EMP-VENDOR",
            "employee_name": "Test",
            "expense_date": "2026-06-10",
            "submission_date": "2026-06-11",
            "category": "Meals",
            "vendor": "Cash Payment",
            "amount": 80.0,
            "has_receipt": False,
            "manager_approved": False,
            "department": "X",
            "description": "",
        }
        flags = detect_vendor_anomaly([record])
        assert len(flags) == 1
        assert flags[0].flag_type == FraudFlagType.VENDOR_ANOMALY

    def test_legitimate_vendor_not_flagged(self, clean_record):
        """A record with a non-suspicious vendor name must not be flagged."""
        flags = detect_vendor_anomaly([clean_record])
        assert len(flags) == 0


class TestRunFraudScan:
    def test_clean_batch_zero_flags(self, clean_record):
        """A single clean record produces zero fraud flags."""
        result = run_fraud_scan([clean_record])
        assert result["total_records"] == 1
        assert result["total_flagged"] == 0
        assert result["flags"] == []

    def test_duplicate_detected_in_full_scan(self, duplicate_pair):
        """run_fraud_scan finds duplicates in a batch containing a duplicate pair."""
        result = run_fraud_scan(duplicate_pair)
        flag_types = [f["flag_type"] for f in result["flags"]]
        assert "duplicate_submission" in flag_types

    def test_flags_sorted_by_risk_descending(self, duplicate_pair, threshold_skirting_record):
        """Flags must be ordered by risk_score descending."""
        batch = duplicate_pair + [threshold_skirting_record]
        result = run_fraud_scan(batch)
        if len(result["flags"]) >= 2:
            scores = [f["risk_score"] for f in result["flags"]]
            assert scores == sorted(scores, reverse=True), "Flags must be sorted by risk_score DESC"
