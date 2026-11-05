FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY llm ./llm
COPY agents ./agents
COPY core ./core
COPY shared ./shared
COPY executer ./executer
COPY apps ./apps
RUN uv sync --locked --no-dev && useradd --uid 10001 --create-home spas \
    && install -d -o spas -g spas -m 0700 /home/spas/.local/state/spas
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 EXECUTOR_LOG_DIR=/tmp/executor-logs
USER 10001:10001
CMD ["uvicorn", "apps.mvp.service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
