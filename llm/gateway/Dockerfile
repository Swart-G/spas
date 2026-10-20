FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_LINK_MODE=copy uv sync --locked --no-dev --extra claude --extra colab --no-install-project
COPY llm /app/llm
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_LINK_MODE=copy uv sync --locked --no-dev --extra claude --extra colab
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home --uid 10001 spas
RUN install -d -o spas -g spas -m 0700 /home/spas/.local/state/spas/llm
USER spas
ENV SPAS_GATEWAY_STATE_DIR=/home/spas/.local/state/spas/llm
EXPOSE 8008
ENTRYPOINT ["spas-llm"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8008"]
