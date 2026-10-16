# syntax=docker/dockerfile:1.7
# Build from the repository root (the shared library lives in libs/):
#   docker build -f services/policy_compiler/Dockerfile .
# ---- build stage: resolve the locked dependency set with uv -------------------------
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY libs/acs_common /app/libs/acs_common
WORKDIR /app/services/policy_compiler
COPY services/policy_compiler/pyproject.toml services/policy_compiler/uv.lock services/policy_compiler/README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable
COPY services/policy_compiler/src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime stage: minimal, non-root, read-only friendly ---------------------------
FROM python:3.12-slim AS runtime
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
ENV PATH="/app/services/policy_compiler/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PORT=8080
# Non-editable install: the venv is self-contained, no source tree needed at runtime.
COPY --from=build --chown=root:root /app/services/policy_compiler/.venv /app/services/policy_compiler/.venv
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "acs_policy_compiler.main"]
