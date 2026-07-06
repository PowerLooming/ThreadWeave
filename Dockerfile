# ── Stage 1: Build ──────────────────────────────────────────
FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

# System deps for ChromaDB (sqlite3, build tools for hnswlib)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps in a venv (non-editable — we copy src separately)
COPY pyproject.toml .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir . && \
    /opt/venv/bin/pip install --no-cache-dir mempalace httpx pydantic

# ── Stage 2: Runtime ────────────────────────────────────────
FROM python:3.11-slim-bookworm

# ChromaDB runtime deps only (no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /opt/venv /opt/venv

# Copy source
WORKDIR /app
COPY pyproject.toml .
COPY src/ src/

# Create a non-root user
RUN useradd --create-home --shell /bin/bash threadweave && \
    mkdir -p /app/data /app/palace && \
    chown -R threadweave:threadweave /app /opt/venv
USER threadweave

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    THREADWEAVE_HOME=/app/data

VOLUME ["/app/palace", "/app/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1

CMD ["uvicorn", "threadweave.api:app", "--host", "0.0.0.0", "--port", "8000"]
