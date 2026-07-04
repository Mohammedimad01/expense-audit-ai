# ExpenseAudit AI — Architecture

> **Kaggle × Google AI Agents Capstone 2026 — Track: Agents for Business**

---

## Core Design Principle

Every dollar-amount decision (over limit? duplicate? statistical outlier?) is made by **tested, deterministic Python code — never by an LLM guessing at arithmetic**.

The LLM agents call those tools, then explain the results in clear, decision-oriented language a non-technical Finance Director can act on in five minutes.

---

## Full Agent Pipeline Flow

```mermaid
flowchart TD
    Client(["fa:fa-user Client\n(curl / Swagger / CLI)"])

    subgraph API["FastAPI Service — api/main.py"]
        Det["POST /audit/deterministic\n(no API key needed)"]
        Full["POST /audit/full\n(requires GOOGLE_API_KEY)\n\nRetry: 2× | Timeout: 120 s\nHTTP 504 on timeout\nHTTP 503 on auth failure\nHTTP 500 after max retries"]
        Trail["GET /audit/trail"]
    end

    subgraph DET_ENGINES["Deterministic Engines (always run first)"]
        PE["policy_engine.py\nrun_policy_check()\n\n• category_limit_exceeded\n  (+ dept multipliers)\n• missing_receipt\n• approval_threshold\n• weekend_expense (LOW)"]
        FE["fraud_engine.py\nrun_fraud_scan()\n\n• duplicate_submission (9)\n• vendor_anomaly (8)\n• split_transaction (8)\n• threshold_skirting (4–7)\n• statistical_outlier (6)\n• round_number_padding (5)"]
        SB["tools.py\nrun_summary_build()\n\n• risk_level\n• top_issues\n• recommended_actions"]
    end

    subgraph PIPELINE["SequentialAgent — orchestrator.py"]
        P1["① policy_compliance_agent\n(LlmAgent)\nNarrates violations,\nseparates hard vs LOW flags"]
        P2["② fraud_pattern_agent\n(LlmAgent)\nRisk-ranks fraud flags,\nexplains each pattern type"]
        P3["③ summary_report_agent\n(LlmAgent)\nProduces executive summary\nfor Finance Director"]
        State[["Session State\nbatch / policy_result\nfraud_result / summary\ndept_multipliers"]]
    end

    AT[("audit_trail.jsonl\nBatch metadata only\nNo PII")]
    DR["Drive Export\n(MCPToolset)\nOptional"]

    Client -->|JSON batch| API
    Det --> PE & FE --> SB
    Full -->|"Pre-run deterministic\n(grounding)"| PE & FE --> SB
    Full --> PIPELINE
    State <-->|"read/write"| P1 & P2 & P3
    P1 -->|tool call| PE
    P2 -->|tool call| FE
    P3 -->|tool call| SB
    API -->|log metadata| AT
    P3 -->|optional| DR
    API -->|JSON response| Client
```

---

## Session State — Data Flow

| Field | Written by | Read by |
|---|---|---|
| `batch` | Orchestrator (initial) | policy_agent, fraud_agent |
| `batch_id` | Orchestrator (initial) | report_agent |
| `department_multipliers` | Orchestrator (initial) | policy_agent (via tool) |
| `policy_result` | Orchestrator pre-run → policy_agent tool | fraud_agent, report_agent |
| `fraud_result` | Orchestrator pre-run → fraud_agent tool | report_agent |
| `summary` | Orchestrator pre-run → report_agent tool | (returned to caller) |

> **Grounding strategy:** The orchestrator runs all three deterministic engines *before* launching the LLM agents and injects results into `initial_state`. This means the LLM agents always narrate pre-computed, test-verified numbers — they cannot hallucinate dollar amounts or counts.

---

## Technology Stack

| Layer | Technology |
|---|---|
| Agent orchestration | [Google ADK](https://google.github.io/adk-docs/) `SequentialAgent` + `LlmAgent` |
| LLM model | Gemini 2.0 Flash (`gemini-2.0-flash`) |
| Deterministic engines | Pure Python (`statistics`, `datetime` stdlib) |
| API service | FastAPI 0.115 + Uvicorn |
| Data validation | Pydantic v2 |
| MCP integration | `google.adk.tools.mcp_tool.MCPToolset` (Google Drive export) |
| Security | HMAC pseudonymisation + append-only JSONL audit trail |
| Testing | pytest 9+ |

---

## Policy Rules (v2 — Day 2 additions)

| Rule | Threshold | Severity |
|---|---|---|
| Meals spend limit | $50 (× dept multiplier) | HIGH |
| Travel spend limit | $1,500 (× dept multiplier) | HIGH |
| Lodging spend limit | $300 (× dept multiplier) | HIGH |
| Office Supplies limit | $200 (× dept multiplier) | HIGH |
| Client Entertainment limit | $250 (× dept multiplier) | HIGH |
| Software/Subscriptions limit | $100 (× dept multiplier) | HIGH |
| Manager pre-approval required | ≥ $500 | HIGH |
| Receipt required | Always | HIGH |
| **Weekend expense flag** *(Day 2)* | Saturday or Sunday | **LOW** |

**Department multipliers (configurable):** e.g. `{"Sales": 1.5, "Marketing": 1.2}` — departments not listed default to 1.0×.

---

## Fraud Detection Rules (v2 — Day 2 additions)

| Pattern | Risk Score | Detection Method |
|---|---|---|
| Duplicate / near-duplicate submission | 9/10 | Exact match on (employee, category, amount, vendor, date) |
| Vendor anomaly (shell-company patterns) | 8/10 | Keyword match against known suspicious patterns |
| **Split transaction** *(Day 2)* | 8/10 | Group by (employee, category, date); sum < threshold individually but ≥ threshold combined |
| Threshold skirting ($450–$499.99 band) | 4–7/10 | Amount range check; risk scales with count |
| **Statistical outlier** *(Day 2)* | 6/10 | Leave-one-out z-score > 2.5 vs. employee's own baseline (≥ 4 expenses required) |
| Round-number padding (≥ 3 exact amounts) | 5/10 | Integer-amount frequency per employee |

---

## Security Architecture

```mermaid
flowchart LR
    ENV[".env\n(gitignored)"] -->|GOOGLE_API_KEY\nPSEUDONYM_SALT| APP

    subgraph APP["Application"]
        RAW["Raw expense records\n(employee_name, amounts)"]
        PSEUDO["pseudonymize.py\nHMAC-SHA256 employee IDs"]
        LOG["audit_trail.py\nbatch_id, counts, mode\nNO individual expense data"]
        LLM["LLM Agents\nRedacted names in prompts"]
    end

    RAW -->|"PII never logged\nor exported raw"| PSEUDO
    PSEUDO -->|"pseudonymised IDs only"| LLM
    APP -->|"batch metadata only"| LOG
```

- **No hardcoded secrets** — all credentials via environment variables
- **Employee PII redacted** — names stripped from all API responses; employee IDs HMAC-pseudonymised in exports
- **Append-only audit trail** — batch-level metadata only; individual expense data never written
- **LLM grounding** — financial figures come from deterministic tools, not LLM inference

---

## Error Handling — `/audit/full`

```mermaid
flowchart TD
    REQ["POST /audit/full"] --> CHECK{API key\nconfigured?}
    CHECK -->|No| E503["HTTP 503\nService Unavailable\n→ use /audit/deterministic"]
    CHECK -->|Yes| ATTEMPT["Attempt pipeline\n(asyncio.wait_for 120s)"]
    ATTEMPT -->|Success| SUCCESS["HTTP 200 + full report"]
    ATTEMPT -->|TimeoutError| E504["HTTP 504 Gateway Timeout\nAudit trail: error=TimeoutError"]
    ATTEMPT -->|EnvironmentError| E503b["HTTP 503\nAudit trail: error=EnvironmentError"]
    ATTEMPT -->|Other error| RETRY{Attempt < 3?}
    RETRY -->|Yes| BACKOFF["Sleep 1s / 2s\n(exponential back-off)"] --> ATTEMPT
    RETRY -->|No - all retries exhausted| E500["HTTP 500\nAudit trail: error=ExcType retries=2"]
```

---

*Generated for Kaggle × Google AI Agents Capstone 2026 — Mohammed Imad Thotan*
