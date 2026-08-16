# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# scipy/pandas wheels cover most platforms, but keep a C toolchain available as a fallback
# so a wheel-less arch doesn't fail the build outright.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (own layer) so code-only changes don't bust the pip cache.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

COPY angel_auto ./angel_auto
COPY config/config.yaml config/strategies.yaml ./config/
COPY scripts ./scripts

# config/.env (real secrets) and data_store/ (SQLite DB, logs, scrip master cache) are
# NOT baked into the image - they're mounted at runtime, see docker-compose.yml.
RUN mkdir -p /app/data_store

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=3).status==200 else 1)"

CMD ["python", "scripts/run_dashboard.py"]
