# syntax=docker/dockerfile:1.7
# Build from the repository root (the shared library lives in libs/):
#   docker build -f services/auditor/Dockerfile .
# ---- build stage: resolve the locked dependency set with uv -------------------------
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY libs/acs_common /app/libs/acs_common
WORKDIR /app/services/auditor
COPY services/auditor/pyproject.toml services/auditor/uv.lock services/auditor/README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable
COPY services/auditor/src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime stage: non-root, with Chromium's headless shell for Playwright ----------
FROM python:3.12-slim AS runtime
ENV PATH="/app/services/auditor/.venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PORT=8080
COPY --from=build --chown=root:root /app/services/auditor/.venv /app/services/auditor/.venv
# Only the headless shell (not full Chromium) keeps the image small; --with-deps installs
# the shared libraries it needs. Browsers are root-owned and read-only for the app user.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && python -m playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "acs_auditor.main"]
