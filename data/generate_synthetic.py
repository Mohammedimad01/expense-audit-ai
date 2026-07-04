#!/usr/bin/env python3
"""
data/generate_synthetic.py
----------------------------
Generates a realistic synthetic expense batch for testing and demo purposes.

Output: data/sample_batch.json
Format: {"department_multipliers": {...}, "records": [...]}

Composition (46 records total):
  - 20 clean records (fully compliant)
  - 10 policy violations:
      - 4 × category limit exceeded
      - 3 × missing receipt
      - 3 × approval threshold without manager sign-off
  - 8 original fraud patterns:
      - 2 × duplicate submission pair (= 4 records)
      - 2 × round-number padding (employees with 3+ round amounts)
      - 1 × threshold-skirting (x2 records for same employee)
      - 1 × vendor anomaly (x2 records)
  - NEW: 2 × split_transaction seed (4 records — 2 pairs)
  - NEW: 1 × statistical_outlier seed (6 records — 5 normal + 1 extreme)
  - Weekend dates are seeded into several records to exercise that check.

DEPARTMENT_MULTIPLIERS constant is embedded in the output envelope so that
the CLI automatically picks it up and applies per-department limits.

Run: python data/generate_synthetic.py
"""

from __future__ import annotations

import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

# Try Faker, fall back to deterministic values if not installed
try:
    from faker import Faker
    _faker = Faker(seed=42)
    def _name(): return _faker.name()
    def _company(): return _faker.company()
    def _word(): return _faker.word()
except ImportError:
    _faker = None
    _NAMES = ["Alice Chen", "Bob Martinez", "Carol Kim", "David Osei", "Elena Ross",
              "Frank Liu", "Grace Patel", "Henry Brown", "Isla Nguyen", "Jack Torres"]
    def _name(): return random.choice(_NAMES)
    def _company(): return f"{random.choice(['Acme','Global','Apex'])} {random.choice(['Corp','Inc','LLC'])}"
    def _word(): return random.choice(["conference","supplies","software","lunch","travel"])

random.seed(42)

CATEGORIES = [
    "Meals", "Travel", "Lodging", "Office Supplies",
    "Client Entertainment", "Software/Subscriptions",
]

LIMITS = {
    "Meals": 50.0,
    "Travel": 1500.0,
    "Lodging": 300.0,
    "Office Supplies": 200.0,
    "Client Entertainment": 250.0,
    "Software/Subscriptions": 100.0,
}

DEPARTMENTS = ["Engineering", "Sales", "Marketing", "Finance", "Operations"]

# Per-department category-limit multipliers embedded in the output envelope.
# Sales gets 1.5x Client Entertainment (client-facing role).
# Marketing gets 1.2x across the board.
DEPARTMENT_MULTIPLIERS: dict[str, float] = {
    "Sales": 1.5,
    "Marketing": 1.2,
}

BASE_DATE = date(2026, 6, 1)
_eid_counter = 1
_exp_counter = 1


def next_eid() -> str:
    global _eid_counter
    v = f"EMP-{_eid_counter:03d}"
    _eid_counter += 1
    return v


def next_expid() -> str:
    global _exp_counter
    v = f"EXP-{_exp_counter:04d}"
    _exp_counter += 1
    return v


def random_date(start: date = BASE_DATE, days: int = 28) -> str:
    return (start + timedelta(days=random.randint(0, days))).isoformat()


def random_weekend_date(start: date = BASE_DATE, days: int = 28) -> str:
    """Return an ISO date string guaranteed to fall on Saturday or Sunday."""
    for _ in range(200):
        d = start + timedelta(days=random.randint(0, days))
        if d.weekday() in (5, 6):
            return d.isoformat()
    # Fallback: first Saturday from start
    d = start
    while d.weekday() != 5:
        d += timedelta(days=1)
    return d.isoformat()


def clean_record(emp_id: str, emp_name: str, dept: str) -> dict:
    """Generate a fully compliant expense record."""
    category = random.choice(CATEGORIES)
    limit = LIMITS[category]
    amount = round(random.uniform(limit * 0.2, limit * 0.9), 2)
    expense_date = random_date()
    return {
        "expense_id": next_expid(),
        "employee_id": emp_id,
        "employee_name": emp_name,
        "submission_date": random_date(date.fromisoformat(expense_date), 3),
        "expense_date": expense_date,
        "category": category,
        "vendor": _company(),
        "amount": amount,
        "description": f"{_word()} expense",
        "has_receipt": True,
        "manager_approved": True if amount >= 500 else random.choice([True, False]),
        "department": dept,
    }


def violation_limit(emp_id: str, emp_name: str, dept: str) -> dict:
    """Record exceeding category limit."""
    category = random.choice(CATEGORIES)
    limit = LIMITS[category]
    amount = round(random.uniform(limit * 1.1, limit * 2.5), 2)
    expense_date = random_date()
    return {
        "expense_id": next_expid(),
        "employee_id": emp_id,
        "employee_name": emp_name,
        "submission_date": random_date(date.fromisoformat(expense_date), 3),
        "expense_date": expense_date,
        "category": category,
        "vendor": _company(),
        "amount": amount,
        "description": f"Over-limit {category.lower()} expense",
        "has_receipt": True,
        "manager_approved": True,
        "department": dept,
    }


def violation_no_receipt(emp_id: str, emp_name: str, dept: str) -> dict:
    """Record without a receipt."""
    category = random.choice(CATEGORIES)
    limit = LIMITS[category]
    amount = round(random.uniform(limit * 0.3, limit * 0.8), 2)
    expense_date = random_date()
    return {
        "expense_id": next_expid(),
        "employee_id": emp_id,
        "employee_name": emp_name,
        "submission_date": random_date(date.fromisoformat(expense_date), 3),
        "expense_date": expense_date,
        "category": category,
        "vendor": _company(),
        "amount": amount,
        "description": "Expense without receipt",
        "has_receipt": False,
        "manager_approved": True,
        "department": dept,
    }


def violation_threshold(emp_id: str, emp_name: str, dept: str) -> dict:
    """Record >= $500 without manager approval."""
    category = random.choice(["Travel", "Lodging", "Client Entertainment"])
    amount = round(random.uniform(500.0, 900.0), 2)
    expense_date = random_date()
    return {
        "expense_id": next_expid(),
        "employee_id": emp_id,
        "employee_name": emp_name,
        "submission_date": random_date(date.fromisoformat(expense_date), 3),
        "expense_date": expense_date,
        "category": category,
        "vendor": _company(),
        "amount": amount,
        "description": "High-value expense without pre-approval",
        "has_receipt": True,
        "manager_approved": False,
        "department": dept,
    }


def fraud_duplicate_pair(emp_id: str, emp_name: str, dept: str) -> list[dict]:
    """Two identical records (duplicate submission fraud)."""
    category = random.choice(CATEGORIES)
    limit = LIMITS[category]
    amount = round(random.uniform(limit * 0.3, limit * 0.8), 2)
    vendor = _company()
    expense_date = random_date()
    sub_date = random_date(date.fromisoformat(expense_date), 3)
    base = {
        "employee_id": emp_id,
        "employee_name": emp_name,
        "submission_date": sub_date,
        "expense_date": expense_date,
        "category": category,
        "vendor": vendor,
        "amount": amount,
        "description": "Business lunch",
        "has_receipt": True,
        "manager_approved": False,
        "department": dept,
    }
    r1 = dict(base); r1["expense_id"] = next_expid()
    r2 = dict(base); r2["expense_id"] = next_expid()
    return [r1, r2]


def fraud_round_numbers(emp_id: str, emp_name: str, dept: str, count: int = 3) -> list[dict]:
    """Several round-dollar submissions from same employee."""
    records = []
    for _ in range(count):
        category = random.choice(CATEGORIES)
        limit = LIMITS[category]
        amount = float(random.choice([25, 30, 40, 50, 75, 100, 150, 200]))
        amount = min(amount, limit * 0.95)
        expense_date = random_date()
        records.append({
            "expense_id": next_expid(),
            "employee_id": emp_id,
            "employee_name": emp_name,
            "submission_date": random_date(date.fromisoformat(expense_date), 3),
            "expense_date": expense_date,
            "category": category,
            "vendor": _company(),
            "amount": float(amount),
            "description": "Round-number expense",
            "has_receipt": True,
            "manager_approved": False,
            "department": dept,
        })
    return records


def fraud_threshold_skirting(emp_id: str, emp_name: str, dept: str, count: int = 2) -> list[dict]:
    """Submissions just under the $500 approval threshold."""
    records = []
    for _ in range(count):
        amount = round(random.uniform(470.0, 499.99), 2)
        expense_date = random_date()
        records.append({
            "expense_id": next_expid(),
            "employee_id": emp_id,
            "employee_name": emp_name,
            "submission_date": random_date(date.fromisoformat(expense_date), 3),
            "expense_date": expense_date,
            "category": "Client Entertainment",
            "vendor": _company(),
            "amount": amount,
            "description": "Client meeting expenses",
            "has_receipt": True,
            "manager_approved": False,
            "department": dept,
        })
    return records


def fraud_vendor_anomaly(emp_id: str, emp_name: str, dept: str, count: int = 2) -> list[dict]:
    """Submissions to suspicious / shell-company vendors."""
    suspicious_vendors = [
        "Cash Payment",
        "Misc Services LLC",
        "Cash Reimbursement",
        "Enterprises Holdings",
        "Solutions Ventures",
    ]
    records = []
    for _ in range(count):
        vendor = random.choice(suspicious_vendors)
        category = random.choice(CATEGORIES)
        amount = round(random.uniform(50.0, 200.0), 2)
        expense_date = random_date()
        records.append({
            "expense_id": next_expid(),
            "employee_id": emp_id,
            "employee_name": emp_name,
            "submission_date": random_date(date.fromisoformat(expense_date), 3),
            "expense_date": expense_date,
            "category": category,
            "vendor": vendor,
            "amount": amount,
            "description": "Vendor payment",
            "has_receipt": False,
            "manager_approved": False,
            "department": dept,
        })
    return records


def fraud_split_transaction(emp_id: str, emp_name: str, dept: str) -> list[dict]:
    """Two same-day, same-category expenses each below $500 that together exceed it.

    Seed: two $280 Travel expenses on the same day = $560 combined >= $500 threshold.
    Each piece looks innocuous individually.
    """
    expense_date = random_date()
    sub_date = random_date(date.fromisoformat(expense_date), 2)
    pieces = [280.00, 280.00]
    records = []
    for i, amount in enumerate(pieces, start=1):
        records.append({
            "expense_id": next_expid(),
            "employee_id": emp_id,
            "employee_name": emp_name,
            "submission_date": sub_date,
            "expense_date": expense_date,  # SAME date — that's the tell
            "category": "Travel",
            "vendor": _company(),
            "amount": amount,
            "description": f"Travel segment {i}",
            "has_receipt": True,
            "manager_approved": False,
            "department": dept,
        })
    return records


def fraud_statistical_outlier(emp_id: str, emp_name: str, dept: str) -> list[dict]:
    """Five normal Meals expenses (~$25-$40) plus one extreme outlier ($480).

    The outlier has a z-score well above 2.5 against the employee's own baseline.
    We need >=4 total expenses for the detector to engage.
    """
    records = []
    normal_amounts = [28.50, 32.00, 35.75, 27.00, 31.25]
    for i, amount in enumerate(normal_amounts, start=1):
        expense_date = random_date()
        records.append({
            "expense_id": next_expid(),
            "employee_id": emp_id,
            "employee_name": emp_name,
            "submission_date": random_date(date.fromisoformat(expense_date), 2),
            "expense_date": expense_date,
            "category": "Meals",
            "vendor": _company(),
            "amount": amount,
            "description": f"Team lunch {i}",
            "has_receipt": True,
            "manager_approved": False,
            "department": dept,
        })
    # The outlier — a Meals expense nearly 15x higher than their baseline
    expense_date = random_date()
    records.append({
        "expense_id": next_expid(),
        "employee_id": emp_id,
        "employee_name": emp_name,
        "submission_date": random_date(date.fromisoformat(expense_date), 2),
        "expense_date": expense_date,
        "category": "Meals",
        "vendor": _company(),
        "amount": 480.00,  # z-score ≈ (480 - 30.9) / stdev_others >> 2.5
        "description": "Team celebration dinner",
        "has_receipt": True,
        "manager_approved": False,
        "department": dept,
    })
    return records


def generate_batch() -> list[dict]:
    records: list[dict] = []

    # --- 20 clean records ---
    for _ in range(20):
        emp = next_eid()
        records.append(clean_record(emp, _name(), random.choice(DEPARTMENTS)))

    # --- 10 policy violations ---
    # 4 × over-limit
    for _ in range(4):
        emp = next_eid()
        records.append(violation_limit(emp, _name(), random.choice(DEPARTMENTS)))

    # 3 × missing receipt
    for _ in range(3):
        emp = next_eid()
        records.append(violation_no_receipt(emp, _name(), random.choice(DEPARTMENTS)))

    # 3 × approval threshold
    for _ in range(3):
        emp = next_eid()
        records.append(violation_threshold(emp, _name(), random.choice(DEPARTMENTS)))

    # --- 8 original fraud patterns ---
    # 2 × duplicate pairs (4 records)
    for _ in range(2):
        emp = next_eid()
        records.extend(fraud_duplicate_pair(emp, _name(), random.choice(DEPARTMENTS)))

    # 1 employee × 3 round-number records
    emp = next_eid()
    records.extend(fraud_round_numbers(emp, _name(), "Finance", 3))

    # 1 × threshold skirting (2 records)
    emp = next_eid()
    records.extend(fraud_threshold_skirting(emp, _name(), "Sales", 2))

    # 1 × vendor anomaly (2 records for same employee)
    emp = next_eid()
    records.extend(fraud_vendor_anomaly(emp, _name(), "Operations", 2))

    # --- NEW: split transaction (2 pairs = 4 records) ---
    for _ in range(2):
        emp = next_eid()
        records.extend(fraud_split_transaction(emp, _name(), random.choice(DEPARTMENTS)))

    # --- NEW: statistical outlier (1 employee, 6 records) ---
    emp = next_eid()
    records.extend(fraud_statistical_outlier(emp, _name(), "Engineering"))

    random.shuffle(records)
    return records


def main() -> None:
    output_path = Path(__file__).parent / "sample_batch.json"
    batch = generate_batch()

    # Write as envelope so CLI auto-picks up department_multipliers
    envelope = {
        "department_multipliers": DEPARTMENT_MULTIPLIERS,
        "records": batch,
    }
    output_path.write_text(
        json.dumps(envelope, indent=2, default=str),
        encoding="utf-8",
    )

    total_amount = sum(r["amount"] for r in batch)
    print(f"Generated {len(batch)} expense records -> {output_path}")
    print(f"Total batch amount: ${total_amount:,.2f}")
    print(f"Department multipliers: {DEPARTMENT_MULTIPLIERS}")
    print(f"  Clean records      : {sum(1 for r in batch if r['has_receipt'] and r['amount'] < 500)}")
    print(f"  Split-tx seeds     : 2 pairs (4 records)")
    print(f"  Outlier seeds      : 1 employee × 6 records")


if __name__ == "__main__":
    main()
