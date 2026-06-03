# ==============================================================================
# Production-Hardened Dockerfile for Voicebot Backend Services (uv sync edition)
# ==============================================================================
FROM python:3.13-slim

# 1. Standard Python Environment Best Practices
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    # Force uv to copy files instead of symlinking or hardlinking inside Docker filesystem
    UV_LINK_MODE=copy \
    # Automatically add the virtual environment's bin/ to system PATH
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 2. Install System Dependencies & Purge Build Tools in a Single Layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    libc-dev \
    && rm -rf /var/lib/apt/lists/*

# 3. Leverage super-fast 'uv' package installer
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

# 4. Install project dependencies securely using uv sync (Frozen & reproducible)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-cache --no-install-project

# 5. Clean up build-only compilers to shrink attack surface
RUN apt-get purge -y --auto-remove gcc libc-dev

# 6. Create a safe non-root system user and change ownership
RUN groupadd -r django && useradd -r -g django -s /bin/false django \
    && chown -R django:django /app

# 7. Copy remaining project files securely
COPY --chown=django:django . .

# 8. Configure entrypoint permissions
RUN chmod +x /app/entrypoint.sh

# 9. Run as the non-privileged user
USER django

# Expose ports for Daphne unified HTTP/WS server
EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]

