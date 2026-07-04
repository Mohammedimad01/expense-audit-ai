"""
tests/conftest.py
------------------
Shared test fixtures for ExpenseAudit AI test suite.
All fixtures use minimal, hardcoded records — no Faker, no randomness.
"""

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Ground-truth expense records
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def clean_record():
    """A fully compliant expense record — should trigger zero violations/flags."""
    return {
        "expense_id": "EXP-CLEAN-001",
        "employee_id": "EMP-CLEAN",
        "employee_name": "Alice Good",
        "submission_date": "2026-06-05",
        "expense_date": "2026-06-03",
        "category": "Meals",
        "vendor": "Noodle House",
        "amount": 35.00,
        "description": "Team lunch",
        "has_receipt": True,
        "manager_approved": False,
        "department": "Engineering",
    }


@pytest.fixture
def over_limit_meals_record():
    """Meals expense exceeding the $50 limit."""
    return {
        "expense_id": "EXP-LIMIT-001",
        "employee_id": "EMP-LIMIT",
        "employee_name": "Bob Spender",
        "submission_date": "2026-06-05",
        "expense_date": "2026-06-03",
        "category": "Meals",
        "vendor": "Fancy Restaurant",
        "amount": 75.00,
        "description": "Client dinner",
        "has_receipt": True,
        "manager_approved": False,
        "department": "Sales",
    }


@pytest.fixture
def missing_receipt_record():
    """Travel expense with no receipt attached."""
    return {
        "expense_id": "EXP-NORECEIPT-001",
        "employee_id": "EMP-NORECEIPT",
        "employee_name": "Carol Forgetful",
        "submission_date": "2026-06-06",
        "expense_date": "2026-06-04",
        "category": "Travel",
        "vendor": "Uber",
        "amount": 45.00,
        "description": "Airport transfer",
        "has_receipt": False,
        "manager_approved": True,
        "department": "Marketing",
    }


@pytest.fixture
def approval_threshold_record():
    """Expense >= $500 without manager pre-approval."""
    return {
        "expense_id": "EXP-THRESH-001",
        "employee_id": "EMP-THRESH",
        "employee_name": "Dave Bypasser",
        "submission_date": "2026-06-07",
        "expense_date": "2026-06-05",
        "category": "Lodging",
        "vendor": "Grand Hotel",
        "amount": 550.00,
        "description": "Conference hotel",
        "has_receipt": True,
        "manager_approved": False,
        "department": "Operations",
    }


@pytest.fixture
def duplicate_pair():
    """Two identical records from the same employee — should trigger duplicate flag."""
    base = {
        "employee_id": "EMP-DUP",
        "employee_name": "Eve Duplicator",
        "submission_date": "2026-06-10",
        "expense_date": "2026-06-08",
        "category": "Meals",
        "vendor": "The Burger Place",
        "amount": 42.00,
        "description": "Team lunch",
        "has_receipt": True,
        "manager_approved": False,
        "department": "Finance",
    }
    r1 = {**base, "expense_id": "EXP-DUP-001"}
    r2 = {**base, "expense_id": "EXP-DUP-002"}
    return [r1, r2]


@pytest.fixture
def round_number_batch():
    """Three round-dollar submissions from the same employee."""
    emp = "EMP-ROUND"
    emp_name = "Frank Padder"
    return [
        {
            "expense_id": f"EXP-ROUND-00{i}",
            "employee_id": emp,
            "employee_name": emp_name,
            "submission_date": "2026-06-10",
            "expense_date": f"2026-06-0{i}",
            "category": "Meals",
            "vendor": f"Restaurant {i}",
            "amount": float(amt),
            "description": "Lunch",
            "has_receipt": True,
            "manager_approved": False,
            "department": "Engineering",
        }
        for i, amt in enumerate([25, 30, 40], start=1)
    ]


@pytest.fixture
def threshold_skirting_record():
    """Single record just under the $500 approval threshold."""
    return {
        "expense_id": "EXP-SKIRT-001",
        "employee_id": "EMP-SKIRT",
        "employee_name": "Grace Skirter",
        "submission_date": "2026-06-12",
        "expense_date": "2026-06-10",
        "category": "Client Entertainment",
        "vendor": "Golf Club",
        "amount": 499.00,
        "description": "Client golf outing",
        "has_receipt": True,
        "manager_approved": False,
        "department": "Sales",
    }
