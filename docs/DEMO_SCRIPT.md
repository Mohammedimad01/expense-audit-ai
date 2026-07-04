# ExpenseAudit AI — Demo Script

> **Target length:** ~5 minutes  
> **Format:** Screen-recording with narration  
> **Tool needed:** Terminal + browser (localhost:8000/docs)

---

## Scene 1 — The Problem (0:00–0:45)

**Show:** A spreadsheet of expense rows (open `data/sample_batch.json` in a JSON viewer or VS Code preview).

**Say:**
> "Finance teams process hundreds of expense reports a month — manually.
> Each one gets reviewed line by line against spend policies and checked
> for patterns that might indicate fraud.
> With 50 records, that's already overwhelming. At 500, it's impossible.
> ExpenseAudit AI automates the entire review pipeline using three
> specialised AI agents working in sequence."

---

## Scene 2 — Architecture (0:45–1:15)

**Show:** `docs/architecture.md` rendered in the browser (GitHub or VS Code markdown preview), or the ASCII diagram from `README.md`.

**Point out:**
- Three LlmAgents in a SequentialAgent pipeline
- Each agent calls a **deterministic Python tool** — no LLM arithmetic
- Output flows policy → fraud → executive summary

**Say:**
> "The key design choice: LLMs explain, Python decides.
> Every amount, every count, every risk score is computed by tested,
> auditable Python code — never estimated by the LLM."

---

## Scene 3 — Deterministic CLI Run (1:15–2:30)

**Run in terminal:**
```bash
python cli.py --input data/sample_batch.json --mode deterministic
```

**Call out these numbers on screen as they appear:**

```
Records reviewed    : 51
Total batch amount  : $10,401.53
Amount flagged      : $16,044.13
Policy violations   : 29
Fraud flags         : 11
Risk level          : CRITICAL
```

**Scroll to fraud flags and highlight:**

| What to point at | What to say |
|---|---|
| `Risk 9/10 \| duplicate_submission \| EXP-0033, EXP-0034` | "Duplicate submission — same $62.49 expense submitted twice, 0 days apart. That's a $124.98 loss if not caught." |
| `Risk 8/10 \| split_transaction \| EXP-0038, EXP-0039` | "Split transaction — $491 + $472 Client Entertainment on the same day. Combined $963 exceeds the $500 approval threshold — but each piece individually doesn't." |
| `Risk 6/10 \| statistical_outlier \| EXP-0051` | "Statistical outlier — $480 Meals for an employee whose baseline is $30.90. Z-score of 148. That's 15× their own average." |

**Scroll to policy violations and highlight:**

| What to point at | What to say |
|---|---|
| EXP-0030 `category_limit_exceeded` $848.97 | "Marketing employee: Client Entertainment $848.97 against a $300 limit (1.2× multiplier). Overage of $548.97." |
| EXP-0028 `approval_threshold_exceeded` $694.40 | "High-value expense submitted without manager sign-off — hard policy breach." |

**Say:**
> "No API key required for this run. The entire analysis completes in under 2 seconds."

---

## Scene 4 — Full LLM Pipeline via API (2:30–3:45)

**Open browser to `http://localhost:8000/docs`**

**Say:**
> "Now let's run the full multi-agent pipeline. The API endpoint is rate-limited
> to protect the paid Gemini API — 10 calls per minute per IP."

**Expand `POST /audit/full` in Swagger UI and paste this payload:**

```json
{
  "submitted_by": "demo",
  "records": [
    {
      "expense_id": "EXP-0033",
      "employee_id": "EMP-031",
      "employee_name": "Demo User",
      "submission_date": "2026-06-05",
      "expense_date": "2026-06-04",
      "category": "Software/Subscriptions",
      "vendor": "Smith LLC",
      "amount": 64.49,
      "description": "SaaS tool",
      "has_receipt": true,
      "manager_approved": false,
      "department": "Engineering"
    },
    {
      "expense_id": "EXP-0034",
      "employee_id": "EMP-031",
      "employee_name": "Demo User",
      "submission_date": "2026-06-05",
      "expense_date": "2026-06-04",
      "category": "Software/Subscriptions",
      "vendor": "Smith LLC",
      "amount": 64.49,
      "description": "SaaS tool",
      "has_receipt": true,
      "manager_approved": false,
      "department": "Engineering"
    }
  ]
}
```

**While it runs (~30 seconds), say:**
> "Three LLM agents are now running in sequence.
> Agent 1 narrates the policy compliance findings.
> Agent 2 provides the fraud risk analysis.
> Agent 3 writes the executive summary."

**Show the JSON response, scroll to `llm_executive_summary` and read a sentence aloud.**

---

## Scene 5 — Security & Deployability (3:45–4:30)

**Show:** The terminal, run:
```bash
# Health check — confirms Drive MCP status, LLM capability
curl http://localhost:8000/health
```

**Show:** `Dockerfile` in editor, point at:
- `FROM python:3.12-slim AS builder` — lean two-stage build
- `USER appuser` — non-root for security
- `HEALTHCHECK` directive

**Say:**
> "The service is containerised with a two-stage Docker build, runs as a
> non-root user, and has a built-in health check.
> All secrets pass through `--env-file .env` — never hard-coded anywhere."

**Show:** `expense_audit/security/pseudonymize.py` briefly.

**Say:**
> "Employee IDs are HMAC-pseudonymised before any export.
> Names are stripped entirely. The audit trail only stores batch-level counts —
> no individual expense data ever hits the log."

---

## Scene 6 — Tests Pass (4:30–5:00)

**Run in terminal:**
```bash
python -m pytest tests/ -v --tb=short -q
```

**Show the final line:**
```
89 passed, 7 warnings in X.XXs
```

**Say:**
> "89 tests covering policy rules, fraud detectors, API endpoints, and agent
> pipeline wiring — all green. The 7 warnings are upstream deprecations in
> ADK and httpx, not issues in this codebase."

---

## Key Numbers to Emphasise

| Metric | Value |
|---|---|
| Records audited | 51 |
| Batch total | $10,401.53 |
| Amount flagged | $16,044.13 |
| Policy violations | 29 |
| Fraud flags | 11 |
| Risk level | CRITICAL |
| Tests | 89 passed |
| Audit time (deterministic) | < 2 seconds |
| Audit time (full LLM) | ~30-60 seconds |

---

## Fallback Plan (if LLM pipeline is slow)

If the `/audit/full` call is taking too long during recording, cut to a pre-recorded response JSON and narrate from it. The deterministic mode is the reliable demo path — it always finishes in under 2 seconds.
