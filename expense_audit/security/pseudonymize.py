"""
expense_audit/security/pseudonymize.py
-----------------------------------------
One-way employee-ID pseudonymisation.

Why: The audit report may be exported to Google Drive or logged to disk.
We never want real employee identifiers in those artefacts — only a stable
pseudonym that Finance can reference internally but that carries no PII.

Method: HMAC-SHA256 with a secret salt loaded from the environment.
  - Deterministic: same employee_id always maps to the same pseudonym.
  - One-way: cannot reverse the pseudonym without the salt.
  - Stable: pseudonyms survive across audit runs (useful for trend analysis).

Production requirements:
  Set PSEUDONYM_SALT to a high-entropy random string, e.g.:
      python -c "import secrets; print(secrets.token_hex(32))"
  If PSEUDONYM_SALT is absent or set to the placeholder value
  "your_random_secret_salt_here", a runtime-only random salt is used and a
  WARNING is emitted. Pseudonyms will NOT be stable across process restarts —
  acceptable for local dev but UNSAFE for production (you can no longer
  cross-reference pseudonyms across audit runs).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets

logger = logging.getLogger(__name__)

# Placeholder value from .env.example — treated as "not set" for safety
_PLACEHOLDER_SALT = "your_random_secret_salt_here"

_RUNTIME_FALLBACK_SALT: str = secrets.token_hex(32)
_warned_about_missing_salt = False


def _get_salt() -> bytes:
    """
    Return the HMAC salt as bytes.

    Precedence:
      1. PSEUDONYM_SALT env var (non-empty, not the .env.example placeholder)
      2. Runtime-only random salt with a loud WARNING (dev / CI only)

    SECURITY WARNING: If PSEUDONYM_SALT is left as the default placeholder
    value ("your_random_secret_salt_here") this function treats it as
    unset and falls back to the random runtime salt.  This is intentional —
    using a publicly-known placeholder in production would make pseudonyms
    trivially reversible.
    """
    global _warned_about_missing_salt
    salt = os.environ.get("PSEUDONYM_SALT", "").strip()

    if not salt or salt == _PLACEHOLDER_SALT:
        if not _warned_about_missing_salt:
            if salt == _PLACEHOLDER_SALT:
                logger.warning(
                    "[SECURITY] PSEUDONYM_SALT is set to the default .env.example "
                    "placeholder value. This is UNSAFE for production -- any attacker "
                    "with the placeholder value can reverse pseudonyms. "
                    "Generate a real secret: "
                    'python -c "import secrets; print(secrets.token_hex(32))"'
                )
            else:
                logger.warning(
                    "PSEUDONYM_SALT is not set.  Using a runtime-only random salt — "
                    "pseudonyms will NOT be stable across process restarts.  "
                    "Set PSEUDONYM_SALT in your .env for production use."
                )
            _warned_about_missing_salt = True
        salt = _RUNTIME_FALLBACK_SALT
    return salt.encode("utf-8")


def pseudonymize_id(employee_id: str) -> str:
    """
    Return a stable, one-way pseudonym for an employee ID.

    Args:
        employee_id: Raw employee identifier (e.g., "EMP-042").

    Returns:
        8-character hex pseudonym (e.g., "a3f7c901").
    """
    digest = hmac.new(
        _get_salt(),
        employee_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:8]


def pseudonymize_batch(records: list[dict]) -> list[dict]:
    """
    Return a copy of the batch with employee_id pseudonymised and
    employee_name removed in every record.

    Args:
        records: List of expense record dicts.

    Returns:
        New list of dicts with PII stripped.
    """
    result = []
    for record in records:
        sanitised = dict(record)
        if "employee_id" in sanitised:
            sanitised["employee_id"] = pseudonymize_id(sanitised["employee_id"])
        # Strip name entirely
        sanitised.pop("employee_name", None)
        result.append(sanitised)
    return result
