FROM python:3.12-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Install dependencies before copying application sources so source-only changes reuse this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini .env.example README.md ./
COPY alembic ./alembic
COPY game_docs ./game_docs
COPY resources ./resources
COPY scripts ./scripts
COPY src ./src

RUN uv sync --frozen --no-dev \
    && chmod +x /app/scripts/docker/*.sh


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    RAG_WORKING_DIR=/app/rag_storage

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl libpq5 \
    && groupadd --system --gid 10001 myagent \
    && useradd --system --uid 10001 --gid myagent --home-dir /app --shell /usr/sbin/nologin myagent \
    && mkdir -p /app/rag_storage \
    && chown -R myagent:myagent /app \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder --chown=myagent:myagent /app/.venv /app/.venv
COPY --from=builder --chown=myagent:myagent /app/alembic.ini /app/alembic.ini
COPY --from=builder --chown=myagent:myagent /app/alembic /app/alembic
COPY --from=builder --chown=myagent:myagent /app/game_docs /app/game_docs
COPY --from=builder --chown=myagent:myagent /app/resources /app/resources
COPY --from=builder --chown=myagent:myagent /app/scripts /app/scripts
COPY --from=builder --chown=myagent:myagent /app/src /app/src

USER myagent

EXPOSE 8000

ENTRYPOINT ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
