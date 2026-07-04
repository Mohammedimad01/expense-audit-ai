"""
expense_audit/engine/fraud_engine.py
--------------------------------------
Deterministic fraud-pattern detection engine.
NO LLM is used here — all fraud scores are produced by pure Python logic
so that every flag can be traced to a specific rule during an audit.

Patterns detected:
  1. Duplicate / near-duplicate submissions
  2. Round-number padding (suspiciously frequent round-dollar amounts)
  3. Threshold-skirting (amounts just under the approval threshold)
  4. Vendor anomaly (names matching shell-vendor / cash-out keyword patterns)
  5. Split transaction (same employee splits one large expense into same-day
     smaller pieces to dodge the approval threshold)
  6. Statistical outlier (z-score > 2.5 vs. employee's own spending baseline,
     using leave-one-out to prevent self-masking)
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from expense_audit.config import get_settings
from expense_audit.models import FraudFlag, FraudFlagType, FraudResult

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Vendor anomaly keyword list
# Realistic shell-company / cash-out indicators used in corporate fraud research
# ──────────────────────────────────────────────────────────────────────────────
_SUSPICIOUS_VENDOR_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bcash\b", re.IGNORECASE),
    re.compile(r"\bpetty\s*cash\b", re.IGNORECASE),
    re.compile(r"\bvenmo\b", re.IGNORECASE),
    re.compile(r"\bzelle\b", re.IGNORECASE),
    re.compile(r"\bpaypal\s*me\b", re.IGNORECASE),
    re.compile(r"\bconsulting\s*(llc|inc|co|corp)?\b", re.IGNORECASE),
    re.compile(r"\bservices?\s*(llc|inc|co|corp)?\b", re.IGNORECASE),
    re.compile(r"\bsolutions?\s*(llc|inc|co|corp)?\b", re.IGNORECASE),
    re.compile(r"\benterprises?\b", re.IGNORECASE),
    re.compile(r"\bholdings?\b", re.IGNORECASE),
    re.compile(r"\bventures?\b", re.IGNORECASE),
    re.compile(r"\bN/?A\b", re.IGNORECASE),
    re.compile(r"\bunknown\b", re.IGNORECASE),
    re.compile(r"\bmisc(ellaneous)?\b", re.IGNORECASE),
]


def _is_suspicious_vendor(vendor: str) -> bool:
    return any(p.search(vendor) for p in _SUSPICIOUS_VENDOR_PATTERNS)


def _is_round_number(amount: float) -> bool:
    """True if the amount is a whole-dollar figure (no cents)."""
    return amount == int(amount)


# ──────────────────────────────────────────────────────────────────────────────
# Individual pattern detectors
# ──────────────────────────────────────────────────────────────────────────────

def detect_duplicates(batch: list[dict[str, Any]]) -> list[FraudFlag]:
    """
    Flag exact and near-duplicate submissions:
    same (employee_id, category, amount, vendor) within duplicate_window_days.
    """
    settings = get_settings()
    window = timedelta(days=settings.duplicate_window_days)
    flags: list[FraudFlag] = []

    # Group by (employee_id, category, amount, vendor)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for record in batch:
        try:
            key = (
                record["employee_id"],
                record.get("category", ""),
                round(float(record.get("amount", 0)), 2),
                record.get("vendor", "").strip().lower(),
            )
            groups[key].append(record)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Could not parse record for duplicate detection: %s", exc)

    for key, records in groups.items():
        if len(records) < 2:
            continue
        # Check pairs within time window
        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                try:
                    date_i = date.fromisoformat(str(records[i]["expense_date"]))
                    date_j = date.fromisoformat(str(records[j]["expense_date"]))
                    if abs(date_i - date_j) <= window:
                        expense_ids = sorted(
                            {records[i]["expense_id"], records[j]["expense_id"]}
                        )
                        flags.append(
                            FraudFlag(
                                expense_ids=expense_ids,
                                employee_id=key[0],
                                flag_type=FraudFlagType.DUPLICATE,
                                detail=(
                                    f"Duplicate submission detected: ${key[2]:.2f} at "
                                    f"'{records[i].get('vendor', '')}' for category '{key[1]}' "
                                    f"submitted {abs((date_i - date_j).days)} day(s) apart"
                                ),
                                risk_score=9,
                                total_amount=key[2] * len(expense_ids),
                            )
                        )
                except (KeyError, ValueError, TypeError) as exc:
                    logger.warning("Date parse error in duplicate detection: %s", exc)
    return flags


def detect_round_numbers(batch: list[dict[str, Any]]) -> list[FraudFlag]:
    """
    Flag employees who submit suspiciously many round-dollar amounts.
    Legitimate expenses rarely end in .00 at high frequency.
    """
    settings = get_settings()
    threshold = settings.round_number_min_count
    flags: list[FraudFlag] = []

    # Group by employee
    employee_records: dict[str, list[dict]] = defaultdict(list)
    for record in batch:
        employee_records[record["employee_id"]].append(record)

    for emp_id, records in employee_records.items():
        round_records = [r for r in records if _is_round_number(float(r.get("amount", 0)))]
        if len(round_records) >= threshold:
            flags.append(
                FraudFlag(
                    expense_ids=[r["expense_id"] for r in round_records],
                    employee_id=emp_id,
                    flag_type=FraudFlagType.ROUND_NUMBER,
                    detail=(
                        f"{len(round_records)} of {len(records)} submissions are exact "
                        f"round-dollar amounts — statistically unusual pattern "
                        f"({len(round_records)/len(records)*100:.0f}% round-number rate)"
                    ),
                    risk_score=5,
                    total_amount=sum(float(r.get("amount", 0)) for r in round_records),
                )
            )
    return flags


def detect_threshold_skirting(batch: list[dict[str, Any]]) -> list[FraudFlag]:
    """
    Flag submissions with amounts just below the manager-approval threshold
    (a common technique to avoid oversight).
    """
    settings = get_settings()
    lower = settings.threshold_skirting_lower
    upper = settings.threshold_skirting_upper
    flags: list[FraudFlag] = []

    # Group skirting records by employee
    employee_skirting: dict[str, list[dict]] = defaultdict(list)
    for record in batch:
        amount = float(record.get("amount", 0))
        if lower <= amount <= upper:
            employee_skirting[record["employee_id"]].append(record)

    for emp_id, records in employee_skirting.items():
        # Single instance of near-threshold warrants a flag (risk 4)
        # Multiple instances within the band are higher risk (7)
        risk = 7 if len(records) > 1 else 4
        flags.append(
            FraudFlag(
                expense_ids=[r["expense_id"] for r in records],
                employee_id=emp_id,
                flag_type=FraudFlagType.THRESHOLD_SKIRTING,
                detail=(
                    f"{len(records)} submission(s) in the ${lower:.0f}–${upper:.2f} range "
                    f"just below the ${settings.approval_threshold:.0f} pre-approval threshold"
                ),
                risk_score=risk,
                total_amount=sum(float(r.get("amount", 0)) for r in records),
            )
        )
    return flags


def detect_vendor_anomaly(batch: list[dict[str, Any]]) -> list[FraudFlag]:
    """
    Flag submissions with vendor names matching known shell-company / cash-out patterns.
    """
    flags: list[FraudFlag] = []
    employee_anomalous: dict[str, list[dict]] = defaultdict(list)

    for record in batch:
        vendor = record.get("vendor", "")
        if _is_suspicious_vendor(vendor):
            employee_anomalous[record["employee_id"]].append(record)

    for emp_id, records in employee_anomalous.items():
        flags.append(
            FraudFlag(
                expense_ids=[r["expense_id"] for r in records],
                employee_id=emp_id,
                flag_type=FraudFlagType.VENDOR_ANOMALY,
                detail=(
                    f"{len(records)} submission(s) to vendor(s) matching "
                    f"shell-company / cash-out patterns: "
                    f"{', '.join(sorted({r.get('vendor','') for r in records}))}"
                ),
                risk_score=8,
                total_amount=sum(float(r.get("amount", 0)) for r in records),
            )
        )
    return flags


def detect_split_transactions(batch: list[dict[str, Any]]) -> list[FraudFlag]:
    """
    Detect expense-splitting: an employee submits 2+ same-category, same-day
    expenses that are each individually below the approval threshold, but whose
    combined total meets or exceeds it.

    This pattern indicates deliberate fragmentation to dodge manager pre-approval.
    """
    settings = get_settings()
    threshold = settings.approval_threshold
    flags: list[FraudFlag] = []

    # Group by (employee_id, category, expense_date)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for record in batch:
        try:
            key = (
                record["employee_id"],
                record.get("category", ""),
                str(record.get("expense_date", "")),
            )
            groups[key].append(record)
        except (KeyError, TypeError) as exc:
            logger.warning("Could not parse record for split-transaction detection: %s", exc)

    for (emp_id, category, exp_date), records in groups.items():
        if len(records) < 2:
            continue

        amounts = [float(r.get("amount", 0)) for r in records]

        # All individual amounts must be below the threshold
        if any(a >= threshold for a in amounts):
            continue

        total = sum(amounts)
        if total >= threshold:
            flags.append(
                FraudFlag(
                    expense_ids=sorted(r["expense_id"] for r in records),
                    employee_id=emp_id,
                    flag_type=FraudFlagType.SPLIT_TRANSACTION,
                    detail=(
                        f"{len(records)} {category} expenses on {exp_date} each below "
                        f"the ${threshold:.0f} approval threshold "
                        f"(${', $'.join(f'{a:.2f}' for a in amounts)}) but combined "
                        f"total ${total:.2f} meets or exceeds it — "
                        f"possible deliberate split to avoid manager pre-approval"
                    ),
                    risk_score=8,
                    total_amount=total,
                )
            )
    return flags


def detect_statistical_outliers(batch: list[dict[str, Any]]) -> list[FraudFlag]:
    """
    Flag expenses that are statistical outliers relative to the same employee's
    other expenses, using a leave-one-out z-score.

    Algorithm (per expense):
      1. Collect all OTHER expenses for the same employee (exclude the expense
         itself from the baseline — otherwise a genuine outlier inflates the
         mean/stdev and masks its own detection).
      2. Require at least 3 other expenses (total employee history >= 4) to
         have a meaningful baseline; skip otherwise.
      3. Compute population stdev of the others.  If stdev == 0 (all identical),
         skip (z-score undefined).
      4. z = (amount - mean_others) / stdev_others.  Flag if |z| > 2.5.

    Risk score: 6 (meaningful but not definitive — warrants review, not accusation).
    """
    flags: list[FraudFlag] = []

    # Group all expenses by employee
    employee_records: dict[str, list[dict]] = defaultdict(list)
    for record in batch:
        try:
            employee_records[record["employee_id"]].append(record)
        except (KeyError, TypeError) as exc:
            logger.warning("Could not index record for outlier detection: %s", exc)

    for emp_id, records in employee_records.items():
        # Need at least 4 total to produce a meaningful leave-one-out baseline
        if len(records) < 4:
            continue

        all_amounts = [float(r.get("amount", 0)) for r in records]

        for i, record in enumerate(records):
            # Leave-one-out: exclude the current expense from the baseline
            other_amounts = all_amounts[:i] + all_amounts[i + 1:]

            if len(other_amounts) < 3:
                continue  # paranoia guard

            mean_others = statistics.mean(other_amounts)
            stdev_others = statistics.pstdev(other_amounts)

            if stdev_others == 0:
                continue  # all other expenses identical — z-score undefined

            z_score = (all_amounts[i] - mean_others) / stdev_others

            if abs(z_score) > 2.5:
                flags.append(
                    FraudFlag(
                        expense_ids=[record["expense_id"]],
                        employee_id=emp_id,
                        flag_type=FraudFlagType.STATISTICAL_OUTLIER,
                        detail=(
                            f"Expense ${all_amounts[i]:.2f} is a statistical outlier "
                            f"for {emp_id} (z-score {z_score:+.2f} vs. employee baseline "
                            f"mean=${mean_others:.2f}, stdev=${stdev_others:.2f} "
                            f"across {len(other_amounts)} other expense(s))"
                        ),
                        risk_score=6,
                        total_amount=all_amounts[i],
                    )
                )
    return flags


# ──────────────────────────────────────────────────────────────────────────────
# Public API — called by FunctionTool wrapper
# ──────────────────────────────────────────────────────────────────────────────

def run_fraud_scan(batch: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Run all fraud-detection patterns against a batch of expense records.

    Args:
        batch: List of expense record dicts (matching ExpenseRecord schema).

    Returns:
        FraudResult serialised as a dict.
    """
    logger.info("Running fraud scan on %d records", len(batch))
    all_flags: list[FraudFlag] = []

    detector_fns = [
        detect_duplicates,
        detect_round_numbers,
        detect_threshold_skirting,
        detect_vendor_anomaly,
        detect_split_transactions,
        detect_statistical_outliers,
    ]

    for detector_fn in detector_fns:
        try:
            flags = detector_fn(batch)
            all_flags.extend(flags)
        except Exception as exc:
            logger.warning("Fraud detector %s raised: %s", detector_fn.__name__, exc)

    # Sort flags by risk score descending
    all_flags.sort(key=lambda f: f.risk_score, reverse=True)

    result = FraudResult(
        total_records=len(batch),
        flags=all_flags,
    )
    logger.info(
        "Fraud scan complete: %d flags found, $%.2f at risk",
        result.total_flagged,
        result.total_at_risk_amount,
    )
    return result.model_dump(mode="json")
