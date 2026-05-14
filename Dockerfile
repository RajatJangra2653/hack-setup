FROM python:3.11-slim AS base

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt requirements-prod.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements-prod.txt

# Application code
COPY src/ src/
COPY routes/ routes/
COPY app.py gunicorn.conf.py startup.sh ./
COPY frontend/ frontend/
COPY samples/ samples/

# Install platform package
RUN pip install --no-cache-dir -e .

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ── Production target ────────────────────────────────────────────
FROM base AS production
ENV ENVIRONMENT=production
CMD ["gunicorn", "app:app", "-c", "gunicorn.conf.py"]

# ── Development target ───────────────────────────────────────────
FROM base AS development
ENV ENVIRONMENT=development
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "dev_server.py"]

# ── FastAPI target (new platform) ────────────────────────────────
FROM base AS platform
ENV ENVIRONMENT=production
CMD ["uvicorn", "platform_core.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
