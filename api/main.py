"""
api/main.py
------------
FastAPI service exposing the ExpenseAudit AI pipeline.

Endpoints:
  GET  /              → Redirect to /docs (Swagger UI)
  GET  /health        → Liveness check with version and capability flags
  POST /audit/deterministic  → Runs engines only (no GOOGLE_API_KEY needed)
  POST /audit/full           → Runs full ADK multi-agent pipeline (LLM required)
  GET  /audit/trail          → Returns last N audit trail entries (metadata only)

All inputs are validated by Pydantic. All outputs are JSON.
Employee names are stripped from all API responses for privacy.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from expense_audit import __version__
from expense_audit.config import get_settings
from expense_audit.engine.fraud_engine import run_fraud_scan
from expense_audit.engine.policy_engine import run_policy_check
from expense_audit.agents.tools import run_summary_build
from expense_audit.models import AuditReport, ExpenseBatch
from expense_audit.security.audit_trail import log_batch_audit, read_audit_trail

logger = logging.getLogger(__name__)

# ── LLM resilience constants ──────────────────────────────────────────────────
# Maximum number of retry attempts for transient LLM failures (excludes first try).
MAX_LLM_RETRIES: int = 2
# Wall-clock timeout in seconds for the entire LLM pipeline run.
LLM_TIMEOUT_SECONDS: int = 120
# Base delay (seconds) for exponential back-off between retries.
_RETRY_BASE_DELAY: float = 1.0

# ── Rate-limit constants for /audit/full (calls a paid external API) ──────────
# Each unique client IP is allowed at most RATE_LIMIT_MAX_CALLS requests to
# /audit/full within a sliding RATE_LIMIT_WINDOW_SECONDS window.
#
# NOTE: This is an in-process, single-worker sliding-window implementation.
# It is suitable for a single-container deployment.  For multi-worker /
# multi-pod deployments, replace _rate_limit_store with a Redis-backed
# counter (e.g. slowapi + redis) so the window is shared across processes.
#
# To disable rate limiting entirely: set RATE_LIMIT_MAX_CALLS=0 in .env
RATE_LIMIT_MAX_CALLS: int = 10        # requests per IP per window
RATE_LIMIT_WINDOW_SECONDS: int = 60  # sliding window width

# {client_ip: deque of UNIX timestamps for recent requests}
_rate_limit_store: dict[str, collections.deque] = collections.defaultdict(
    lambda: collections.deque()
)


def _check_rate_limit(client_ip: str) -> None:
    """
    Enforce sliding-window rate limiting for the /audit/full endpoint.

    Raises HTTP 429 if the client has exceeded RATE_LIMIT_MAX_CALLS within
    the last RATE_LIMIT_WINDOW_SECONDS seconds.

    Args:
        client_ip: The requester's IP address (from request.client.host).
    """
    if RATE_LIMIT_MAX_CALLS <= 0:
        return  # rate limiting disabled

    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    dq = _rate_limit_store[client_ip]

    # Evict timestamps outside the current window
    while dq and dq[0] < window_start:
        dq.popleft()

    if len(dq) >= RATE_LIMIT_MAX_CALLS:
        logger.warning(
            "Rate limit hit for %s (%d calls in %ds window)",
            client_ip, len(dq), RATE_LIMIT_WINDOW_SECONDS,
        )
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded: max {RATE_LIMIT_MAX_CALLS} calls to /audit/full "
                f"per {RATE_LIMIT_WINDOW_SECONDS}s window.  "
                "Use /audit/deterministic for an instant, free result, or retry later."
            ),
            headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
        )

    dq.append(now)

# ──────────────────────────────────────────────────────────────────────────────
# App initialisation
# ──────────────────────────────────────────────────────────────────────────────

# WHY docs_url=None / redoc_url=None:
# Disabling FastAPI's built-in Swagger route lets us serve a fully custom
# HTML page at /docs with our own CSS injected, rather than the stock light
# theme.  The OpenAPI JSON schema at /openapi.json is unaffected — only the
# presentation layer changes.
app = FastAPI(
    title="ExpenseAudit AI",
    description=(
        "Multi-agent expense-report auditing system powered by **Google ADK** "
        "and **Gemini 2.0 Flash**.  \n\n"
        "Detects policy violations and fraud patterns in expense batches using "
        "three sequential LlmAgents backed by deterministic Python engines."
    ),
    version=__version__,
    docs_url=None,     # served manually below with custom dark theme
    redoc_url=None,    # not used — Swagger is the primary UI
    license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
)

# Mount static assets (CSS theme + SVG favicon) at /static.
# Must be done immediately after app creation so the /docs route can
# reference /static/swagger-dark.css and /static/favicon.svg.
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui() -> HTMLResponse:
    """
    Serve Swagger UI with the custom dark fintech theme.

    WHY hand-crafted HTML instead of get_swagger_ui_html()?
    --------------------------------------------------------
    This version of FastAPI's get_swagger_ui_html() only accepts
    swagger_css_url which *replaces* the entire base Swagger CSS —
    meaning our file would have to re-implement all of Swagger's layout
    from scratch, which is fragile and maintenance-heavy.

    Instead we write the HTML directly: load the standard CDN CSS first
    (for layout/structure), then append our override stylesheet as a second
    <link> tag.  Our CSS uses !important declarations to ensure overrides
    take precedence.  This is the pattern recommended by Swagger UI docs
    for theming without forking the base styles.
    """
    openapi_url = app.openapi_url or "/openapi.json"
    swagger_js  = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"
    swagger_css = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"
    preset_js   = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{app.title} — API Docs</title>
  <meta name="description" content="ExpenseAudit AI API — multi-agent expense auditing powered by Google ADK and Gemini 2.0 Flash"/>

  <!-- 1. Swagger UI base CSS (layout + structure) -->
  <link rel="stylesheet" href="{swagger_css}"/>

  <!-- 2. Our dark fintech override CSS (applied on top via !important) -->
  <link rel="stylesheet" href="/static/swagger-dark.css"/>

  <!-- Favicon: circuit-style SVG matching the accent color -->
  <link rel="icon" type="image/svg+xml" href="/static/favicon.svg"/>
</head>
<body>
  <div id="swagger-ui"></div>

  <script src="{swagger_js}" crossorigin></script>
  <script src="{preset_js}" crossorigin></script>
  <script>
    window.onload = function() {{
      SwaggerUIBundle({{
        url: "{openapi_url}",
        dom_id: "#swagger-ui",
        presets: [
          SwaggerUIBundle.presets.apis,
          SwaggerUIStandalonePreset
        ],
        layout: "StandaloneLayout",
        // ── UX parameters ──────────────────────────────────────────────
        // Collapse all by default for a clean first impression;
        // the search filter lets users find endpoints instantly.
        docExpansion:             "list",
        defaultModelsExpandDepth: -1,       // hide Schemas section on load
        defaultModelExpandDepth:  2,
        filter:                   true,     // search bar
        displayRequestDuration:   true,     // show response time (ms)
        tryItOutEnabled:          false,    // require explicit "Try it out" click
        persistAuthorization:     true,     // keep tokens across page reload
        deepLinking:              true,     // anchor links per-endpoint
        showExtensions:           true,
        showCommonExtensions:     true,
      }});
    }};
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)



# ──────────────────────────────────────────────────────────────────────────────
# Request / Response models
# ──────────────────────────────────────────────────────────────────────────────

class AuditRequest(BaseModel):
    batch_id: Optional[str] = Field(
        default=None,
        description="Optional batch ID. Auto-generated if not provided.",
    )
    submitted_by: str = Field(
        default="api_user",
        description="User or system submitting the batch.",
    )
    records: list[dict[str, Any]] = Field(
        ...,
        min_length=1,
        description="List of expense records matching the ExpenseRecord schema.",
    )
    department_multipliers: Optional[dict[str, float]] = Field(
        default=None,
        description=(
            "Optional per-department category-limit multipliers. "
            "E.g. {\"Sales\": 1.5, \"Marketing\": 1.2}. "
            "Departments absent from the dict use 1.0x (no adjustment)."
        ),
    )
    enable_drive_export: bool = Field(
        default=False,
        description=(
            "When True, the report agent will attempt to upload the final report "
            "to Google Drive via MCPToolset.  Requires GOOGLE_DRIVE_MCP_CREDENTIALS "
            "to be set in the server environment. Only applies to /audit/full."
        ),
    )


class HealthResponse(BaseModel):
    status: str
    version: str
    llm_enabled: bool
    drive_mcp_enabled: bool
    timestamp: str


# ──────────────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    """Redirect root to Swagger UI."""
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health():
    """Liveness check — returns version and capability flags."""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        version=__version__,
        llm_enabled=settings.llm_enabled,
        drive_mcp_enabled=settings.drive_mcp_enabled,
        timestamp=datetime.now(tz=timezone.utc).isoformat(),
    )


@app.post("/audit/deterministic", tags=["Audit"])
async def audit_deterministic(request: AuditRequest) -> JSONResponse:
    """
    Run the deterministic audit pipeline (policy + fraud engines only).

    No GOOGLE_API_KEY required. Returns structured JSON in < 2 seconds.
    Employee names are stripped from the response.
    """
    batch_id = request.batch_id or f"BATCH-{uuid.uuid4().hex[:8].upper()}"
    records = request.records

    try:
        policy_result = run_policy_check(records, department_multipliers=request.department_multipliers)
        fraud_result = run_fraud_scan(records)
        summary = run_summary_build(
            batch_id=batch_id,
            policy_result=policy_result,
            fraud_result=fraud_result,
            batch=records,
        )
    except Exception as exc:
        logger.error("Deterministic audit failed for batch %s: %s", batch_id, exc)
        raise HTTPException(status_code=500, detail=f"Audit engine error: {exc}")

    log_batch_audit(
        batch_id=batch_id,
        submitted_by=request.submitted_by,
        record_count=len(records),
        policy_violations=policy_result.get("total_flagged", 0),
        fraud_flags=fraud_result.get("total_flagged", 0),
        mode="deterministic",
    )

    report = AuditReport(
        batch_id=batch_id,
        submitted_by=request.submitted_by,
        audit_timestamp=datetime.now(tz=timezone.utc).isoformat(),
        mode="deterministic",
        policy_result=policy_result,  # type: ignore[arg-type]
        fraud_result=fraud_result,  # type: ignore[arg-type]
        summary=summary,  # type: ignore[arg-type]
    )
    return JSONResponse(content=report.to_redacted_dict())


@app.post("/audit/full", tags=["Audit"])
async def audit_full(request: AuditRequest, req: Request) -> JSONResponse:
    """
    Run the full multi-agent audit pipeline (LLM-powered).

    Requires GOOGLE_API_KEY. Runs policy agent → fraud agent → report agent
    via a SequentialAgent, then returns all three narratives plus structured data.

    Resilience:
    - Rate-limited per IP (RATE_LIMIT_MAX_CALLS / RATE_LIMIT_WINDOW_SECONDS) → HTTP 429
    - Times out after LLM_TIMEOUT_SECONDS (120 s) → HTTP 504
    - Retries transient errors up to MAX_LLM_RETRIES (2) with exponential back-off
    - Logs every failure to the audit trail (batch-level metadata only, no PII)
    """
    # ── Rate limiting (paid external API guard) ────────────────────────────────
    client_ip = req.client.host if req.client else "unknown"
    _check_rate_limit(client_ip)

    settings = get_settings()
    if not settings.llm_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "GOOGLE_API_KEY is not configured. "
                "Use /audit/deterministic for a no-LLM audit, "
                "or set GOOGLE_API_KEY in your .env file."
            ),
        )

    batch_id = request.batch_id or f"BATCH-{uuid.uuid4().hex[:8].upper()}"
    records = request.records

    # Import here to avoid loading ADK at startup when LLM is not needed
    from expense_audit.agents.orchestrator import run_pipeline

    pipeline_result: dict[str, Any] | None = None
    last_exc: Exception | None = None
    attempt = 0

    for attempt in range(MAX_LLM_RETRIES + 1):
        try:
            pipeline_result = await asyncio.wait_for(
                run_pipeline(
                    batch_id=batch_id,
                    batch=records,
                    submitted_by=request.submitted_by,
                    department_multipliers=request.department_multipliers,
                    enable_drive_export=request.enable_drive_export,
                ),
                timeout=LLM_TIMEOUT_SECONDS,
            )
            break  # success

        except asyncio.TimeoutError:
            logger.error(
                "Full pipeline timed out after %ds for batch %s (attempt %d/%d)",
                LLM_TIMEOUT_SECONDS, batch_id, attempt + 1, MAX_LLM_RETRIES + 1,
            )
            log_batch_audit(
                batch_id=batch_id,
                submitted_by=request.submitted_by,
                record_count=len(records),
                policy_violations=-1,
                fraud_flags=-1,
                mode="full",
                extra={"error": "TimeoutError", "attempt": attempt + 1},
            )
            raise HTTPException(
                status_code=504,
                detail=(
                    f"The LLM pipeline did not respond within {LLM_TIMEOUT_SECONDS} seconds. "
                    "Try /audit/deterministic for an instant result, or retry later."
                ),
            )

        except EnvironmentError as exc:
            # Missing API key or similar configuration problem — no point retrying
            logger.error("LLM configuration error for batch %s: %s", batch_id, type(exc).__name__)
            log_batch_audit(
                batch_id=batch_id,
                submitted_by=request.submitted_by,
                record_count=len(records),
                policy_violations=-1,
                fraud_flags=-1,
                mode="full",
                extra={"error": type(exc).__name__, "attempt": attempt + 1},
            )
            raise HTTPException(status_code=503, detail=str(exc))

        except Exception as exc:  # noqa: BLE001
            # Surface Gemini quota errors (429) immediately — no point burning retries
            error_type = type(exc).__name__
            if "ResourceExhausted" in error_type or "429" in str(exc)[:50]:
                logger.warning("Gemini quota exhausted for batch %s (attempt %d)", batch_id, attempt + 1)
                log_batch_audit(
                    batch_id=batch_id,
                    submitted_by=request.submitted_by,
                    record_count=len(records),
                    policy_violations=-1,
                    fraud_flags=-1,
                    mode="full",
                    extra={"error": "ResourceExhausted", "attempt": attempt + 1},
                )
                raise HTTPException(
                    status_code=429,
                    detail=(
                        "Gemini API quota exceeded. "
                        "Please retry after a short wait, or use /audit/deterministic for an instant result."
                    ),
                )
            last_exc = exc
            error_type = type(exc).__name__
            logger.warning(
                "LLM pipeline attempt %d/%d failed for batch %s: %s",
                attempt + 1, MAX_LLM_RETRIES + 1, batch_id, error_type,
            )
            if attempt < MAX_LLM_RETRIES:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                logger.info("Retrying in %.1fs ...", delay)
                await asyncio.sleep(delay)
            else:
                # All retries exhausted — log to audit trail and surface error
                log_batch_audit(
                    batch_id=batch_id,
                    submitted_by=request.submitted_by,
                    record_count=len(records),
                    policy_violations=-1,
                    fraud_flags=-1,
                    mode="full",
                    extra={"error": error_type, "retries": MAX_LLM_RETRIES},
                )
                logger.error(
                    "Full pipeline failed after %d attempts for batch %s: %s",
                    MAX_LLM_RETRIES + 1, batch_id, error_type,
                )
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"LLM pipeline failed after {MAX_LLM_RETRIES + 1} attempts "
                        f"({error_type}). "
                        "Use /audit/deterministic for an instant result."
                    ),
                )

    assert pipeline_result is not None  # guaranteed by loop above

    log_batch_audit(
        batch_id=batch_id,
        submitted_by=request.submitted_by,
        record_count=len(records),
        policy_violations=pipeline_result.get("policy_result", {}).get("total_flagged", 0),
        fraud_flags=pipeline_result.get("fraud_result", {}).get("total_flagged", 0),
        mode="full",
        extra={"attempts": attempt + 1},
    )

    report = AuditReport(
        batch_id=batch_id,
        submitted_by=request.submitted_by,
        audit_timestamp=datetime.now(tz=timezone.utc).isoformat(),
        mode="full",
        policy_result=pipeline_result.get("policy_result"),  # type: ignore[arg-type]
        fraud_result=pipeline_result.get("fraud_result"),  # type: ignore[arg-type]
        summary=pipeline_result.get("summary"),  # type: ignore[arg-type]
        llm_policy_narrative=pipeline_result.get("llm_policy_narrative"),
        llm_fraud_narrative=pipeline_result.get("llm_fraud_narrative"),
        llm_executive_summary=pipeline_result.get("llm_executive_summary"),
    )
    return JSONResponse(content=report.to_redacted_dict())


@app.get("/audit/trail", tags=["Audit"])
async def audit_trail(limit: int = 20) -> JSONResponse:
    """
    Return the most recent audit trail entries.

    Entries contain batch-level metadata only — no individual expense PII.
    """
    entries = read_audit_trail(limit=min(limit, 200))
    return JSONResponse(content={"entries": entries, "count": len(entries)})
