# ThreadWeave API Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system deps for ChromaDB
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]" fastapi uvicorn httpx

# Copy source
COPY src/ src/
COPY tests/ tests/

EXPOSE 8000

CMD ["uvicorn", "threadweave.api:app", "--host", "0.0.0.0", "--port", "8000"]
