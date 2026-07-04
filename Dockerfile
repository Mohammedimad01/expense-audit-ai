# ============================================================
# ExpenseAudit AI — Dockerfile
# Python 3.12-slim, two-stage build for a lean production image
#
# Build:
#   docker build -t expense-audit-ai .
#
# Run (pass secrets via --env-file, never -e in CI logs):
#   docker run -p 8000:8000 --env-file .env expense-audit-ai
#
# Health check:
#   curl http://localhost:8000/health
# ============================================================

# ── Stage 1: Builder ─────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# System build deps (needed by some native wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies into an isolated target directory (no venv overhead in
# multi-stage builds — the output directory is just copied to the runtime stage).
COPY requirements.txt ./
RUN pip install --no-cache-dir --target /build/deps -r requirements.txt

# Copy application source (separate COPY layers so deps layer is cached)
COPY pyproject.toml ./
COPY expense_audit/ ./expense_audit/
COPY api/ ./api/
COPY cli.py ./


# ── Stage 2: Runtime ─────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy installed packages from builder stage
COPY --from=builder /build/deps /usr/local/lib/python3.12/site-packages/

# Copy application source
COPY --from=builder /build/expense_audit ./expense_audit
COPY --from=builder /build/api ./api
COPY --from=builder /build/cli.py ./
COPY data/ ./data/

# ── Non-root user for security ────────────────────────────────
RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

# ── Runtime environment defaults ─────────────────────────────
# Override ALL of these at docker run time via --env-file .env
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8000 \
    LOG_LEVEL=INFO

# Expose the API port
EXPOSE 8000

# ── Health check ─────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
        || exit 1

# ── Default command ───────────────────────────────────────────
# main:app refers to api/main.py::app  (uvicorn resolves relative to PYTHONPATH)
CMD ["python", "-m", "uvicorn", "api.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1"]
