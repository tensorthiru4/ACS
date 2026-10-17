# syntax=docker/dockerfile:1.7
# ---- build stage: resolve the locked dependency set with uv -------------------------
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# ---- runtime stage: minimal, non-root, read-only friendly ---------------------------
FROM python:3.12-slim AS runtime
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PORT=8080
WORKDIR /app
COPY --from=build --chown=root:root /app /app
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "acs_orchestrator.main"]
