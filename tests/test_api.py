"""
tests/test_api.py
------------------
FastAPI smoke tests — verifies the API layer works end-to-end
without requiring a GOOGLE_API_KEY (deterministic endpoint only).
"""

import pytest
from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture
def minimal_batch():
    """Minimal valid batch for API smoke tests."""
    return {
        "submitted_by": "test_suite",
        "records": [
            {
                "expense_id": "EXP-API-001",
                "employee_id": "EMP-API",
                "employee_name": "Test User",
                "submission_date": "2026-06-10",
                "expense_date": "2026-06-08",
                "category": "Meals",
                "vendor": "Test Cafe",
                "amount": 25.00,
                "description": "Lunch",
                "has_receipt": True,
                "manager_approved": False,
                "department": "Engineering",
            }
        ],
    }


@pytest.fixture
def violation_batch():
    """Batch with known violations for response verification."""
    return {
        "submitted_by": "test_suite",
        "records": [
            {
                "expense_id": "EXP-API-LIMIT",
                "employee_id": "EMP-API-BAD",
                "employee_name": "Violator User",
                "submission_date": "2026-06-10",
                "expense_date": "2026-06-08",
                "category": "Meals",
                "vendor": "Expensive Restaurant",
                "amount": 99.99,
                "description": "Over-limit meals",
                "has_receipt": True,
                "manager_approved": False,
                "department": "Sales",
            }
        ],
    }


class TestHealth:
    def test_health_returns_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "llm_enabled" in data
        assert "drive_mcp_enabled" in data
        assert "timestamp" in data

    def test_root_redirects_to_docs(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code in (301, 302, 307, 308)
        assert "/docs" in response.headers["location"]


class TestDeterministicAudit:
    def test_clean_batch_returns_200(self, client, minimal_batch):
        response = client.post("/audit/deterministic", json=minimal_batch)
        assert response.status_code == 200

    def test_clean_batch_has_expected_fields(self, client, minimal_batch):
        response = client.post("/audit/deterministic", json=minimal_batch)
        data = response.json()
        assert "batch_id" in data
        assert "policy_result" in data
        assert "fraud_result" in data
        assert "summary" in data
        assert data["mode"] == "deterministic"

    def test_violation_batch_detected(self, client, violation_batch):
        """Over-limit Meals record must be flagged in the response."""
        response = client.post("/audit/deterministic", json=violation_batch)
        assert response.status_code == 200
        data = response.json()
        policy = data["policy_result"]
        assert policy["total_flagged"] >= 1
        types = [v["violation_type"] for v in policy["violations"]]
        assert "category_limit_exceeded" in types

    def test_response_excludes_employee_name(self, client, minimal_batch):
        """Employee names must NOT appear anywhere in the API response."""
        response = client.post("/audit/deterministic", json=minimal_batch)
        response_text = response.text
        assert "Test User" not in response_text, "Employee name leaked into API response"

    def test_empty_records_rejected(self, client):
        """A batch with zero records must be rejected with 422."""
        response = client.post(
            "/audit/deterministic",
            json={"submitted_by": "test", "records": []},
        )
        assert response.status_code == 422

    def test_batch_id_auto_generated(self, client, minimal_batch):
        """If no batch_id provided, one must be auto-generated."""
        response = client.post("/audit/deterministic", json=minimal_batch)
        data = response.json()
        assert data["batch_id"] is not None
        assert len(data["batch_id"]) > 0

    def test_custom_batch_id_preserved(self, client, minimal_batch):
        """If a batch_id is provided, it must be echoed back."""
        minimal_batch["batch_id"] = "TEST-BATCH-42"
        response = client.post("/audit/deterministic", json=minimal_batch)
        data = response.json()
        assert data["batch_id"] == "TEST-BATCH-42"


class TestAuditTrail:
    def test_audit_trail_endpoint_returns_200(self, client):
        response = client.get("/audit/trail")
        assert response.status_code == 200
        data = response.json()
        assert "entries" in data
        assert "count" in data
        assert isinstance(data["entries"], list)

    def test_audit_trail_no_pii(self, client, minimal_batch):
        """After an audit, the trail must not contain employee names or amounts."""
        client.post("/audit/deterministic", json=minimal_batch)
        response = client.get("/audit/trail")
        trail_text = response.text
        assert "Test User" not in trail_text
        assert "employee_name" not in trail_text
