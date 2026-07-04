"""
expense_audit/models.py
-----------------------
Pydantic data models for the entire system.
All monetary values are Python float (USD).
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# ──────────────────────────────────────────────────────────────────────────────
# Input models
# ──────────────────────────────────────────────────────────────────────────────

class ExpenseCategory(str, Enum):
    MEALS = "Meals"
    TRAVEL = "Travel"
    LODGING = "Lodging"
    OFFICE_SUPPLIES = "Office Supplies"
    CLIENT_ENTERTAINMENT = "Client Entertainment"
    SOFTWARE_SUBSCRIPTIONS = "Software/Subscriptions"
    OTHER = "Other"


class ExpenseRecord(BaseModel):
    """A single line-item on an expense report."""

    expense_id: str = Field(..., description="Unique identifier for this expense")
    employee_id: str = Field(..., description="Submitting employee identifier")
    employee_name: str = Field(..., description="Submitting employee name (redacted in exports)")
    submission_date: date = Field(..., description="Date the expense was submitted")
    expense_date: date = Field(..., description="Date the expense was incurred")
    category: str = Field(..., description="Expense category")
    vendor: str = Field(..., description="Vendor / merchant name")
    amount: float = Field(..., ge=0.0, description="Expense amount in USD")
    description: str = Field(default="", description="Free-text description")
    has_receipt: bool = Field(..., description="Whether a receipt was attached")
    manager_approved: bool = Field(
        default=False, description="Whether manager pre-approval was obtained"
    )
    department: str = Field(default="Unknown", description="Employee department")


class ExpenseBatch(BaseModel):
    """A collection of expense records submitted for auditing."""

    batch_id: str = Field(..., description="Unique batch identifier")
    submitted_by: str = Field(..., description="User who submitted the batch for audit")
    records: list[ExpenseRecord] = Field(..., min_length=1)

    @model_validator(mode="after")
    def deduplicate_expense_ids(self) -> "ExpenseBatch":
        ids = [r.expense_id for r in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("Batch contains duplicate expense_id values")
        return self


# ──────────────────────────────────────────────────────────────────────────────
# Policy engine output models
# ──────────────────────────────────────────────────────────────────────────────

class PolicyViolationType(str, Enum):
    CATEGORY_LIMIT_EXCEEDED = "category_limit_exceeded"
    MISSING_RECEIPT = "missing_receipt"
    APPROVAL_THRESHOLD = "approval_threshold_exceeded"
    WEEKEND_EXPENSE = "weekend_expense"


class PolicyViolation(BaseModel):
    expense_id: str
    employee_id: str
    violation_type: PolicyViolationType
    detail: str
    amount: float
    category: str


class PolicyResult(BaseModel):
    total_records: int
    violations: list[PolicyViolation] = Field(default_factory=list)
    total_flagged: int = 0
    total_flagged_amount: float = 0.0

    @model_validator(mode="after")
    def compute_totals(self) -> "PolicyResult":
        self.total_flagged = len(self.violations)
        self.total_flagged_amount = sum(v.amount for v in self.violations)
        return self


# ──────────────────────────────────────────────────────────────────────────────
# Fraud engine output models
# ──────────────────────────────────────────────────────────────────────────────

class FraudFlagType(str, Enum):
    DUPLICATE = "duplicate_submission"
    ROUND_NUMBER = "round_number_padding"
    THRESHOLD_SKIRTING = "threshold_skirting"
    VENDOR_ANOMALY = "vendor_anomaly"
    SPLIT_TRANSACTION = "split_transaction"
    STATISTICAL_OUTLIER = "statistical_outlier"


class FraudFlag(BaseModel):
    expense_ids: list[str]
    employee_id: str
    flag_type: FraudFlagType
    detail: str
    risk_score: int = Field(ge=1, le=10, description="1=low, 10=critical")
    total_amount: float


class FraudResult(BaseModel):
    total_records: int
    flags: list[FraudFlag] = Field(default_factory=list)
    total_flagged: int = 0
    total_at_risk_amount: float = 0.0

    @model_validator(mode="after")
    def compute_totals(self) -> "FraudResult":
        self.total_flagged = len(self.flags)
        self.total_at_risk_amount = sum(f.total_amount for f in self.flags)
        return self


# ──────────────────────────────────────────────────────────────────────────────
# Summary / report models
# ──────────────────────────────────────────────────────────────────────────────

class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class SummaryResult(BaseModel):
    batch_id: str
    total_records: int
    total_amount: float
    flagged_amount: float
    policy_violations: int
    fraud_flags: int
    risk_level: RiskLevel
    top_issues: list[str]
    recommended_actions: list[str]


class AuditReport(BaseModel):
    """Final output of the full audit pipeline."""

    batch_id: str
    submitted_by: str
    audit_timestamp: str
    mode: str  # "deterministic" or "full"
    policy_result: Optional[PolicyResult] = None
    fraud_result: Optional[FraudResult] = None
    summary: Optional[SummaryResult] = None
    llm_policy_narrative: Optional[str] = None
    llm_fraud_narrative: Optional[str] = None
    llm_executive_summary: Optional[str] = None

    def to_redacted_dict(self) -> dict[str, Any]:
        """Return a dict with employee names stripped — safe for export/logging."""
        data = self.model_dump()
        # Strip any employee_name fields
        def scrub(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {
                    k: scrub(v) for k, v in obj.items()
                    if k not in {"employee_name", "submitted_by"}
                }
            if isinstance(obj, list):
                return [scrub(i) for i in obj]
            return obj
        return scrub(data)
