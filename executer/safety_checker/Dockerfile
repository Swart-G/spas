FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12.14-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=build_ca \
  if [ -f /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
  uv sync --frozen --no-dev --no-default-groups --no-install-project
COPY src ./src
RUN --mount=type=secret,id=build_ca \
  if [ -f /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
  uv sync --frozen --no-dev --no-default-groups --no-editable

FROM python:3.12.14-slim-bookworm
WORKDIR /app
RUN groupadd --gid 10001 analyzer && useradd --uid 10001 --gid analyzer --no-create-home analyzer
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"
CMD ["uvicorn", "safety_analyzer.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
