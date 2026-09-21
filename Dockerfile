FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --locked --no-dev

FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
ENV DATA_DIR=/data DOWNLOAD_DIR=/downloads
WORKDIR /app
COPY --from=build /app /app
COPY docker-entrypoint.sh /usr/local/bin/

VOLUME ["/data", "/downloads"]
EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
