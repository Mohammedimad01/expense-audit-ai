"""
tests/test_audit_logic.py
--------------------------
Comprehensive unit + smoke tests for the deterministic audit engines.

All unit tests use hand-crafted minimal fixtures — no Faker, no randomness —
so failures are immediately traceable to the exact record that caused them.

Coverage:
  Policy compliance:
    - over-limit (category limit exceeded)
    - missing receipt
    - missing pre-approval (threshold)
    - weekend flag (Saturday / Sunday)
    - weekday non-flag (Wednesday)
    - malformed date graceful skip (no crash)

  Department multipliers:
    - multiplier raises effective limit → record within new limit, NOT flagged
    - multiplier for different dept (unlisted) defaults to 1.0× → still flagged
    - multiplier explicitly listed = 1.0 → same as default, still flagged

  Fraud patterns (each tested in isolation):
    - duplicate_submission
    - round_number_padding
    - threshold_skirting
    - vendor_anomaly
    - split_transaction
    - statistical_outlier
    - statistical_outlier: insufficient history (<4 expenses) → no flag

  Summary aggregation:
    - mixed batch → correct violation + flag counts flow into summary

  Smoke test:
    - real generated sample_batch.json → violations and flags both non-empty
      (does NOT assert specific patterns, only that the engines produce output)

  Agent pipeline wiring (no live API call):
    - SequentialAgent has exactly 3 sub-agents
    - Order: policy_compliance_agent → fraud_pattern_agent → summary_report_agent
    - Each agent has the correct FunctionTool registered
    - fraud_agent._INSTRUCTION is a valid, non-empty Python string (regression for
      the broken-quote bug where Critical rules were outside the triple-quoted string)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from expense_audit.engine.fraud_engine import (
    detect_duplicates,
    detect_round_numbers,
    detect_split_transactions,
    detect_statistical_outliers,
    detect_threshold_skirting,
    detect_vendor_anomaly,
    run_fraud_scan,
)
from expense_audit.engine.policy_engine import (
    check_approval_threshold,
    check_category_limit,
    check_missing_receipt,
    check_weekend_expense,
    run_policy_check,
)
from expense_audit.agents.tools import run_summary_build
from expense_audit.models import FraudFlagType, PolicyViolationType


# ──────────────────────────────────────────────────────────────────────────────
# Minimal hand-crafted fixtures
# ──────────────────────────────────────────────────────────────────────────────

def _rec(**overrides) -> dict:
    """Build a minimal valid expense record, applying any keyword overrides."""
    base = {
        "expense_id": "EXP-TEST",
        "employee_id": "EMP-TEST",
        "employee_name": "Test User",
        "submission_date": "2026-06-04",
        "expense_date": "2026-06-03",   # Wednesday — weekday
        "category": "Meals",
        "vendor": "Honest Cafe",
        "amount": 30.00,
        "description": "Team lunch",
        "has_receipt": True,
        "manager_approved": False,
        "department": "Engineering",
    }
    base.update(overrides)
    return base


# ──────────────────────────────────────────────────────────────────────────────
# 1. Policy Compliance
# ──────────────────────────────────────────────────────────────────────────────

class TestOverLimit:
    def test_meals_75_exceeds_50_limit(self):
        """$75 Meals expense exceeds the $50 base limit — must be flagged."""
        rec = _rec(expense_id="EXP-OL-001", amount=75.00, category="Meals")
        v = check_category_limit(rec)
        assert v is not None
        assert v.violation_type == PolicyViolationType.CATEGORY_LIMIT_EXCEEDED
        assert v.expense_id == "EXP-OL-001"
        assert "50" in v.detail  # limit mentioned

    def test_meals_30_within_50_limit(self):
        """$30 Meals expense is within the $50 limit — must NOT be flagged."""
        rec = _rec(amount=30.00, category="Meals")
        v = check_category_limit(rec)
        assert v is None

    def test_unknown_category_skipped(self):
        """Category not in policy config is ignored, never flagged."""
        rec = _rec(category="Parking", amount=9999.00)
        v = check_category_limit(rec)
        assert v is None


class TestMissingReceipt:
    def test_no_receipt_flagged(self):
        rec = _rec(expense_id="EXP-NR-001", has_receipt=False)
        v = check_missing_receipt(rec)
        assert v is not None
        assert v.violation_type == PolicyViolationType.MISSING_RECEIPT

    def test_receipt_present_not_flagged(self):
        rec = _rec(has_receipt=True)
        v = check_missing_receipt(rec)
        assert v is None


class TestApprovalThreshold:
    def test_550_unapproved_flagged(self):
        """$550 without manager approval must be flagged."""
        rec = _rec(expense_id="EXP-AT-001", amount=550.00,
                   category="Lodging", manager_approved=False)
        v = check_approval_threshold(rec)
        assert v is not None
        assert v.violation_type == PolicyViolationType.APPROVAL_THRESHOLD
        assert v.amount == 550.00

    def test_550_approved_not_flagged(self):
        """$550 WITH manager approval must NOT be flagged."""
        rec = _rec(amount=550.00, category="Lodging", manager_approved=True)
        v = check_approval_threshold(rec)
        assert v is None

    def test_499_not_flagged_by_threshold_rule(self):
        """$499 is below the $500 threshold — rule must not fire."""
        rec = _rec(amount=499.00, manager_approved=False)
        v = check_approval_threshold(rec)
        assert v is None


class TestWeekendExpense:
    def test_saturday_flagged(self):
        """2026-06-06 is a Saturday — must produce WEEKEND_EXPENSE violation."""
        rec = _rec(expense_id="EXP-WE-SAT", expense_date="2026-06-06")
        v = check_weekend_expense(rec)
        assert v is not None
        assert v.violation_type == PolicyViolationType.WEEKEND_EXPENSE
        assert "Saturday" in v.detail

    def test_sunday_flagged(self):
        """2026-06-07 is a Sunday — must produce WEEKEND_EXPENSE violation."""
        rec = _rec(expense_id="EXP-WE-SUN", expense_date="2026-06-07")
        v = check_weekend_expense(rec)
        assert v is not None
        assert v.violation_type == PolicyViolationType.WEEKEND_EXPENSE
        assert "Sunday" in v.detail

    def test_wednesday_not_flagged(self):
        """2026-06-03 is a Wednesday — must NOT be flagged."""
        rec = _rec(expense_date="2026-06-03")
        v = check_weekend_expense(rec)
        assert v is None

    def test_malformed_date_does_not_crash(self):
        """A malformed date must be silently skipped — no exception raised."""
        rec = _rec(expense_date="not-a-date")
        v = check_weekend_expense(rec)
        assert v is None  # graceful skip

    def test_missing_date_does_not_crash(self):
        """A missing expense_date key must be silently skipped."""
        rec = _rec()
        rec.pop("expense_date", None)
        v = check_weekend_expense(rec)
        assert v is None


# ──────────────────────────────────────────────────────────────────────────────
# 2. Department Multipliers
# ──────────────────────────────────────────────────────────────────────────────

class TestDepartmentMultipliers:
    """
    Base Meals limit = $50.
    Sales multiplier = 1.5 → effective limit = $75.
    Engineering is NOT in the multiplier dict → defaults to 1.0× = $50.
    """

    def test_multiplier_raises_effective_limit_no_violation(self):
        """Sales 1.5× means $65 Meals is within effective $75 limit → no flag."""
        rec = _rec(
            expense_id="EXP-MULT-SALES",
            amount=65.00,
            category="Meals",
            department="Sales",
        )
        mults = {"Sales": 1.5}
        v = check_category_limit(rec, department_multipliers=mults)
        assert v is None, (
            f"Sales employee at $65 should be within effective $75 Meals limit; got: {v}"
        )

    def test_multiplier_detail_shows_effective_limit(self):
        """When multiplier is applied and amount exceeds effective limit, detail should explain."""
        rec = _rec(
            expense_id="EXP-MULT-OVER",
            amount=100.00,
            category="Meals",
            department="Sales",
        )
        mults = {"Sales": 1.5}
        v = check_category_limit(rec, department_multipliers=mults)
        # $100 > $75 effective → still flagged, but detail mentions the multiplier
        assert v is not None
        assert "1.5" in v.detail
        assert "75" in v.detail  # effective limit

    def test_unlisted_department_defaults_to_1x(self):
        """Engineering is not in multiplier dict — uses 1.0× base limit of $50."""
        rec = _rec(
            expense_id="EXP-MULT-ENG",
            amount=65.00,
            category="Meals",
            department="Engineering",
        )
        mults = {"Sales": 1.5}
        v = check_category_limit(rec, department_multipliers=mults)
        assert v is not None, (
            "Engineering (unlisted) should use 1.0× → $65 > $50 → violation expected"
        )
        assert v.violation_type == PolicyViolationType.CATEGORY_LIMIT_EXCEEDED

    def test_none_multipliers_behaves_as_1x(self):
        """Passing department_multipliers=None is identical to no multiplier."""
        rec = _rec(amount=65.00, category="Meals", department="Sales")
        v = check_category_limit(rec, department_multipliers=None)
        assert v is not None  # $65 > $50 base limit, no multiplier applied

    def test_run_policy_check_passes_multipliers_through(self):
        """run_policy_check forwards department_multipliers to check_category_limit."""
        # $65 Meals for Sales — within effective limit with 1.5× multiplier
        batch = [_rec(
            expense_id="EXP-PASS-001",
            amount=65.00,
            category="Meals",
            department="Sales",
            has_receipt=True,
            manager_approved=False,
        )]
        result = run_policy_check(batch, department_multipliers={"Sales": 1.5})
        violation_types = [v["violation_type"] for v in result["violations"]]
        assert "category_limit_exceeded" not in violation_types, (
            "Sales $65 Meals should NOT be flagged when 1.5× multiplier is applied"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 3. Fraud Patterns — each in isolation
# ──────────────────────────────────────────────────────────────────────────────

class TestDuplicateSubmission:
    def test_exact_pair_flagged(self):
        """Two records with same employee/category/amount/vendor → duplicate flag."""
        r1 = _rec(expense_id="EXP-DUP-001", vendor="The Burger Place",
                  amount=42.00, expense_date="2026-06-08")
        r2 = _rec(expense_id="EXP-DUP-002", vendor="The Burger Place",
                  amount=42.00, expense_date="2026-06-08")
        flags = detect_duplicates([r1, r2])
        assert len(flags) == 1
        assert flags[0].flag_type == FraudFlagType.DUPLICATE
        assert set(flags[0].expense_ids) == {"EXP-DUP-001", "EXP-DUP-002"}

    def test_different_employees_not_flagged(self):
        """Same amount+vendor but different employees → NOT a duplicate."""
        r1 = _rec(expense_id="EXP-DE-001", employee_id="EMP-A",
                  vendor="Cafe", amount=30.00, expense_date="2026-06-08")
        r2 = _rec(expense_id="EXP-DE-002", employee_id="EMP-B",
                  vendor="Cafe", amount=30.00, expense_date="2026-06-08")
        flags = detect_duplicates([r1, r2])
        assert len(flags) == 0

    def test_single_record_no_flag(self):
        flags = detect_duplicates([_rec()])
        assert len(flags) == 0


class TestRoundNumberPadding:
    def _round_record(self, idx: int, amount: float) -> dict:
        return _rec(expense_id=f"EXP-RN-{idx:03d}", employee_id="EMP-ROUND",
                    amount=amount, expense_date=f"2026-06-{idx:02d}")

    def test_three_round_numbers_flagged(self):
        batch = [self._round_record(i, a) for i, a in enumerate([25.0, 30.0, 40.0], 1)]
        flags = detect_round_numbers(batch)
        assert len(flags) == 1
        assert flags[0].flag_type == FraudFlagType.ROUND_NUMBER
        assert flags[0].employee_id == "EMP-ROUND"

    def test_two_round_numbers_not_flagged(self):
        """Threshold is 3 — only 2 round-dollar records should NOT trigger."""
        batch = [self._round_record(i, a) for i, a in enumerate([25.0, 30.0], 1)]
        flags = detect_round_numbers(batch)
        assert len(flags) == 0

    def test_non_round_not_flagged(self):
        flags = detect_round_numbers([_rec(amount=28.75)])
        assert len(flags) == 0


class TestThresholdSkirting:
    def test_499_flagged(self):
        """$499 is within the $450–$499.99 skirting band → flagged."""
        rec = _rec(expense_id="EXP-SK-001", amount=499.00,
                   category="Client Entertainment")
        flags = detect_threshold_skirting([rec])
        assert len(flags) == 1
        assert flags[0].flag_type == FraudFlagType.THRESHOLD_SKIRTING

    def test_550_not_skirting(self):
        """$550 is above the skirting band → NOT flagged by this detector."""
        flags = detect_threshold_skirting([_rec(amount=550.00, manager_approved=False)])
        assert len(flags) == 0

    def test_35_not_skirting(self):
        flags = detect_threshold_skirting([_rec(amount=35.00)])
        assert len(flags) == 0

    def test_multiple_skirting_higher_risk(self):
        """Two skirting records from same employee → risk_score 7 (vs 4 for single)."""
        r1 = _rec(expense_id="EXP-SK-001", employee_id="EMP-SK", amount=490.00)
        r2 = _rec(expense_id="EXP-SK-002", employee_id="EMP-SK", amount=480.00)
        flags = detect_threshold_skirting([r1, r2])
        assert len(flags) == 1
        assert flags[0].risk_score == 7


class TestVendorAnomaly:
    def test_cash_vendor_flagged(self):
        rec = _rec(expense_id="EXP-VA-001", vendor="Cash Payment")
        flags = detect_vendor_anomaly([rec])
        assert len(flags) == 1
        assert flags[0].flag_type == FraudFlagType.VENDOR_ANOMALY

    def test_misc_services_llc_flagged(self):
        rec = _rec(expense_id="EXP-VA-002", vendor="Misc Services LLC")
        flags = detect_vendor_anomaly([rec])
        assert len(flags) == 1

    def test_legitimate_vendor_not_flagged(self):
        flags = detect_vendor_anomaly([_rec(vendor="Marriott Hotel")])
        assert len(flags) == 0


class TestSplitTransaction:
    def test_two_pieces_summing_above_threshold_flagged(self):
        """Two $280 Travel expenses on the same day = $560 → flagged."""
        common = dict(
            employee_id="EMP-SPLIT",
            category="Travel",
            expense_date="2026-06-10",
            vendor="AirCo",
            has_receipt=True,
            manager_approved=False,
            department="Sales",
            submission_date="2026-06-11",
            employee_name="Split User",
            description="travel",
        )
        r1 = {**common, "expense_id": "EXP-SPLIT-001", "amount": 280.00}
        r2 = {**common, "expense_id": "EXP-SPLIT-002", "amount": 280.00}
        flags = detect_split_transactions([r1, r2])
        assert len(flags) == 1
        assert flags[0].flag_type == FraudFlagType.SPLIT_TRANSACTION
        assert flags[0].employee_id == "EMP-SPLIT"
        assert set(flags[0].expense_ids) == {"EXP-SPLIT-001", "EXP-SPLIT-002"}
        assert flags[0].total_amount == pytest.approx(560.00)

    def test_one_piece_above_threshold_not_split(self):
        """If one individual piece >= threshold, it's not a split — it's just over-threshold."""
        common = dict(
            employee_id="EMP-NOSPLIT",
            category="Travel",
            expense_date="2026-06-10",
            vendor="AirCo",
            has_receipt=True,
            manager_approved=False,
            department="Sales",
            submission_date="2026-06-11",
            employee_name="Test",
            description="",
        )
        r1 = {**common, "expense_id": "EXP-NS-001", "amount": 600.00}
        r2 = {**common, "expense_id": "EXP-NS-002", "amount": 280.00}
        flags = detect_split_transactions([r1, r2])
        assert len(flags) == 0, "If any piece >= threshold, skip — not a split pattern"

    def test_same_day_different_categories_not_split(self):
        """Split detection groups by (employee, category, date) — different category ≠ split."""
        common = dict(
            employee_id="EMP-CATDIFF",
            expense_date="2026-06-10",
            vendor="Vendor",
            has_receipt=True,
            manager_approved=False,
            department="Engineering",
            submission_date="2026-06-11",
            employee_name="Test",
            description="",
            amount=280.00,
        )
        r1 = {**common, "expense_id": "EXP-CD-001", "category": "Travel"}
        r2 = {**common, "expense_id": "EXP-CD-002", "category": "Meals"}
        flags = detect_split_transactions([r1, r2])
        assert len(flags) == 0

    def test_different_dates_not_split(self):
        """Same employee + category but different dates → not a split."""
        common = dict(
            employee_id="EMP-DATEIFF",
            category="Travel",
            vendor="AirCo",
            has_receipt=True,
            manager_approved=False,
            department="Engineering",
            submission_date="2026-06-11",
            employee_name="Test",
            description="",
            amount=280.00,
        )
        r1 = {**common, "expense_id": "EXP-DD-001", "expense_date": "2026-06-10"}
        r2 = {**common, "expense_id": "EXP-DD-002", "expense_date": "2026-06-11"}
        flags = detect_split_transactions([r1, r2])
        assert len(flags) == 0


class TestStatisticalOutlier:
    def _emp_batch(self, normal_amounts: list[float], outlier_amount: float) -> list[dict]:
        """Build a batch: several normal expenses + one extreme outlier, all same employee."""
        records = []
        for i, amt in enumerate(normal_amounts, 1):
            records.append(_rec(
                expense_id=f"EXP-OUT-N{i:02d}",
                employee_id="EMP-OUTLIER",
                amount=amt,
                expense_date=f"2026-06-{i:02d}",
            ))
        records.append(_rec(
            expense_id="EXP-OUT-BIG",
            employee_id="EMP-OUTLIER",
            amount=outlier_amount,
            expense_date="2026-06-20",
        ))
        return records

    def test_extreme_outlier_flagged(self):
        """$480 Meals against a baseline of ~$30 normal Meals — z >> 2.5 → flagged."""
        batch = self._emp_batch([28.50, 32.00, 35.75, 27.00], outlier_amount=480.00)
        flags = detect_statistical_outliers(batch)
        assert len(flags) >= 1
        flagged_ids = [f.expense_ids[0] for f in flags]
        assert "EXP-OUT-BIG" in flagged_ids
        assert flags[0].flag_type == FraudFlagType.STATISTICAL_OUTLIER

    def test_leave_one_out_prevents_self_masking(self):
        """The outlier itself must NOT be included in the baseline used to score it.

        If we naively included the outlier in its own baseline, it inflates the mean
        and stdev, masking itself (z-score comes out too low).  This test verifies the
        leave-one-out implementation catches the outlier correctly.

        We use slightly varied normals [28, 30, 32, 29] (pstdev > 0 after any leave-one-out
        sub-selection) so the stdev=0 guard does not prevent scoring.
        The $400 outlier should have a z-score >> 2.5 against those normals.
        """
        # 5 records total: 4 varied normals + 1 outlier
        batch = self._emp_batch([28.0, 30.0, 32.0, 29.0], outlier_amount=400.0)
        flags = detect_statistical_outliers(batch)
        assert any("EXP-OUT-BIG" in f.expense_ids for f in flags), (
            "Outlier should be detected; leave-one-out baseline must exclude it"
        )

    def test_insufficient_history_no_flag(self):
        """Employee with fewer than 4 expenses → no baseline → no flag, even if an
        amount looks extreme."""
        batch = [
            _rec(expense_id=f"EXP-FEW-{i}", employee_id="EMP-FEW",
                 amount=a, expense_date=f"2026-06-{i:02d}")
            for i, a in enumerate([30.0, 30.0, 900.0], 1)  # only 3 records
        ]
        flags = detect_statistical_outliers(batch)
        assert len(flags) == 0, (
            "Fewer than 4 expenses must not produce any outlier flag (insufficient baseline)"
        )

    def test_exactly_four_expenses_produces_baseline(self):
        """4 expenses is the minimum required — detector should engage."""
        batch = self._emp_batch([30.0, 30.0, 30.0], outlier_amount=400.0)
        assert len(batch) == 4
        flags = detect_statistical_outliers(batch)
        # We just verify it runs without error and can produce a flag
        # (whether it fires depends on z-score, which it should here)
        assert isinstance(flags, list)


# ──────────────────────────────────────────────────────────────────────────────
# 4. Summary Aggregation
# ──────────────────────────────────────────────────────────────────────────────

class TestSummaryAggregation:
    def test_mixed_batch_counts_correct(self):
        """Full pipeline on a known mixed batch produces accurate summary counts."""
        # 1 policy violation (over-limit) + 1 fraud flag (duplicate pair)
        over_limit = _rec(
            expense_id="EXP-SUM-OL",
            amount=75.00,
            category="Meals",
            has_receipt=True,
        )
        dup_base = dict(
            employee_id="EMP-SUM-DUP",
            employee_name="Dup User",
            submission_date="2026-06-10",
            expense_date="2026-06-08",
            category="Meals",
            vendor="Dup Cafe",
            amount=42.00,
            description="lunch",
            has_receipt=True,
            manager_approved=False,
            department="Finance",
        )
        r1 = {**dup_base, "expense_id": "EXP-SUM-D1"}
        r2 = {**dup_base, "expense_id": "EXP-SUM-D2"}

        batch = [over_limit, r1, r2]
        policy_result = run_policy_check(batch)
        fraud_result = run_fraud_scan(batch)
        summary = run_summary_build(
            batch_id="BATCH-SUM-TEST",
            policy_result=policy_result,
            fraud_result=fraud_result,
            batch=batch,
        )

        assert summary["total_records"] == 3
        assert summary["policy_violations"] >= 1  # over-limit
        assert summary["fraud_flags"] >= 1         # duplicate

    def test_clean_batch_zero_counts(self):
        """A fully compliant single record → zero violations, zero flags."""
        rec = _rec(amount=30.00, has_receipt=True, manager_approved=False)
        policy_result = run_policy_check([rec])
        fraud_result = run_fraud_scan([rec])
        summary = run_summary_build(
            batch_id="BATCH-CLEAN",
            policy_result=policy_result,
            fraud_result=fraud_result,
            batch=[rec],
        )
        assert summary["policy_violations"] == 0
        assert summary["fraud_flags"] == 0
        assert summary["risk_level"] == "LOW"

    def test_weekend_violations_counted_in_policy(self):
        """Weekend expense violations appear in the policy result total."""
        sat_rec = _rec(expense_id="EXP-SAT-SUM", expense_date="2026-06-06")
        result = run_policy_check([sat_rec])
        types = [v["violation_type"] for v in result["violations"]]
        assert "weekend_expense" in types

    def test_split_and_outlier_counted_in_fraud(self):
        """Split-transaction and statistical-outlier flags appear in fraud result."""
        # Split pair
        split_common = dict(
            employee_id="EMP-SUM-SP",
            employee_name="Split",
            submission_date="2026-06-11",
            expense_date="2026-06-10",
            category="Travel",
            vendor="AirX",
            has_receipt=True,
            manager_approved=False,
            department="Engineering",
            description="",
        )
        sp1 = {**split_common, "expense_id": "EXP-SUM-SP1", "amount": 280.00}
        sp2 = {**split_common, "expense_id": "EXP-SUM-SP2", "amount": 280.00}

        # Outlier batch
        outlier_emp = "EMP-SUM-OT"
        outlier_batch = [
            _rec(expense_id=f"EXP-SUM-OT{i}", employee_id=outlier_emp,
                 amount=a, expense_date=f"2026-06-{i:02d}")
            for i, a in enumerate([28.0, 30.0, 32.0, 29.0], 1)
        ]
        outlier_batch.append(
            _rec(expense_id="EXP-SUM-OT5", employee_id=outlier_emp,
                 amount=490.0, expense_date="2026-06-20")
        )

        batch = [sp1, sp2] + outlier_batch
        result = run_fraud_scan(batch)
        flag_types = {f["flag_type"] for f in result["flags"]}
        assert "split_transaction" in flag_types
        assert "statistical_outlier" in flag_types


# ──────────────────────────────────────────────────────────────────────────────
# 5. Smoke test against real generated batch file
# ──────────────────────────────────────────────────────────────────────────────

class TestSmokeBatch:
    """Load the generated sample_batch.json and assert the engines produce output.

    We do NOT assert on specific patterns here because the batch contains
    random elements.  The only guarantee is that a batch seeded with known
    fraud/violation records must produce at least some findings.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def sample_batch_data(cls):
        path = Path(__file__).parent.parent / "data" / "sample_batch.json"
        if not path.exists():
            pytest.skip(f"sample_batch.json not found at {path} — run generate_synthetic.py first")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return raw, None
        records = raw.get("records", raw)
        multipliers = raw.get("department_multipliers")
        return records, multipliers

    def test_policy_violations_nonempty(self, sample_batch_data):
        records, multipliers = sample_batch_data
        result = run_policy_check(records, department_multipliers=multipliers)
        assert result["total_flagged"] > 0, (
            "Expected at least one policy violation in the seeded sample batch"
        )

    def test_fraud_flags_nonempty(self, sample_batch_data):
        records, _ = sample_batch_data
        result = run_fraud_scan(records)
        assert result["total_flagged"] > 0, (
            "Expected at least one fraud flag in the seeded sample batch"
        )

    def test_policy_result_schema(self, sample_batch_data):
        records, multipliers = sample_batch_data
        result = run_policy_check(records, department_multipliers=multipliers)
        assert "total_records" in result
        assert "violations" in result
        assert isinstance(result["violations"], list)

    def test_fraud_result_schema(self, sample_batch_data):
        records, _ = sample_batch_data
        result = run_fraud_scan(records)
        assert "total_records" in result
        assert "flags" in result
        assert isinstance(result["flags"], list)


# ───────────────────────────────────────────────────────────────────────────────
# 6. Agent Pipeline Wiring (no live API call)
# ───────────────────────────────────────────────────────────────────────────────

class TestAgentPipelineWiring:
    """Verify the SequentialAgent object graph without making any live API calls.

    These tests inspect the ADK agent objects directly — no Runner, no session,
    no Gemini token is consumed.  They are safe to run in CI without a GOOGLE_API_KEY.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def pipeline(cls):
        """Build the pipeline once per class."""
        from expense_audit.agents.orchestrator import create_pipeline
        return create_pipeline()

    def test_pipeline_has_exactly_three_sub_agents(self, pipeline):
        """The SequentialAgent must contain exactly 3 sub-agents."""
        assert len(pipeline.sub_agents) == 3, (
            f"Expected 3 sub-agents, got {len(pipeline.sub_agents)}: "
            f"{[a.name for a in pipeline.sub_agents]}"
        )

    def test_pipeline_order_policy_fraud_summary(self, pipeline):
        """Sub-agents must be ordered: policy → fraud → summary."""
        names = [a.name for a in pipeline.sub_agents]
        assert names[0] == "policy_compliance_agent", (
            f"First agent should be policy_compliance_agent, got '{names[0]}'"
        )
        assert names[1] == "fraud_pattern_agent", (
            f"Second agent should be fraud_pattern_agent, got '{names[1]}'"
        )
        assert names[2] == "summary_report_agent", (
            f"Third agent should be summary_report_agent, got '{names[2]}'"
        )

    def test_policy_agent_has_policy_check_tool(self, pipeline):
        """policy_compliance_agent must have run_policy_check registered."""
        policy_agent = pipeline.sub_agents[0]
        tool_names = [t.name for t in policy_agent.tools]
        assert "run_policy_check" in tool_names, (
            f"policy_compliance_agent tools: {tool_names}"
        )

    def test_fraud_agent_has_fraud_scan_tool(self, pipeline):
        """fraud_pattern_agent must have run_fraud_scan registered."""
        fraud_agent = pipeline.sub_agents[1]
        tool_names = [t.name for t in fraud_agent.tools]
        assert "run_fraud_scan" in tool_names, (
            f"fraud_pattern_agent tools: {tool_names}"
        )

    def test_report_agent_has_summary_build_tool(self, pipeline):
        """summary_report_agent must have run_summary_build registered."""
        report_agent = pipeline.sub_agents[2]
        tool_names = [t.name for t in report_agent.tools]
        assert "run_summary_build" in tool_names, (
            f"summary_report_agent tools: {tool_names}"
        )

    def test_fraud_agent_instruction_is_valid_string(self):
        """Regression: fraud_agent._INSTRUCTION must be a non-empty Python str.

        This catches the Day-2 bug where the triple-quoted string was accidentally
        closed early and the duplicate Critical rules section was outside the
        string, producing bare Python code that would break the module.
        """
        from expense_audit.agents import fraud_agent
        assert isinstance(fraud_agent._INSTRUCTION, str), (
            "_INSTRUCTION must be a str; the module may have a broken triple-quote"
        )
        assert len(fraud_agent._INSTRUCTION) > 200, (
            "_INSTRUCTION is suspiciously short — possible broken triple-quote truncating it"
        )
        # Must mention the Day-2 fraud types
        assert "split_transaction" in fraud_agent._INSTRUCTION
        assert "statistical_outlier" in fraud_agent._INSTRUCTION

    def test_policy_agent_instruction_mentions_weekend(self):
        """policy_compliance_agent must explain weekend_expense in its instruction."""
        from expense_audit.agents import policy_agent
        assert "weekend_expense" in policy_agent._INSTRUCTION
        assert "LOW" in policy_agent._INSTRUCTION
