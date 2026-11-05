FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    EXECUTOR_LOG_DIR=/var/log/executor

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt \
    && groupadd --gid 10001 executor \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /workspace --shell /usr/sbin/nologin executor \
    && mkdir -p /workspace /inputs /var/log/executor \
    && chown 10001:10001 /workspace \
    && chmod 0700 /workspace /var/log/executor \
    && chmod 0555 /inputs \
    && touch /etc/spas-executor \
    && chmod 0444 /etc/spas-executor

COPY executer/__init__.py /app/executer/__init__.py
COPY executer/server_mcp/*.py /app/executer/server_mcp/
COPY executer/safety_checker/*.py /app/executer/safety_checker/
COPY shared/contracts/safety.py /app/shared/contracts/safety.py
RUN chmod -R a-w /app && chown root:10001 /workspace && chmod 0770 /workspace

# Контроллер root имеет только 3 capabilities из Run_docker.sh.
# Процесс команды всегда запускается отдельно как 10001:10001, без дополнительных групп.
USER 0:0
ENTRYPOINT ["python", "-m", "executer.server_mcp.Server"]
