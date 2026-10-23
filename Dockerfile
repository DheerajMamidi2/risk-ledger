# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.12-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 riskledger \
    && useradd --system --uid 10001 --gid riskledger --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin riskledger

WORKDIR /app

COPY --from=builder --chown=riskledger:riskledger /app/.venv /app/.venv

USER riskledger

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).close()"]

ENTRYPOINT ["/app/.venv/bin/uvicorn"]
CMD ["risk_ledger.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
