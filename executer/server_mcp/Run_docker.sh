#!/usr/bin/env bash
# Единый профиль запуска для терминала, MCP-клиента и Codex.
set -euo pipefail

image="${EXECUTOR_IMAGE:-spas-executor:local}"
entry=(python -m executer.server_mcp.Server)
args=()
for argument in "$@"; do
    case "$argument" in
        --manual) entry=(python -m executer.server_mcp.Manual) ;;
        --without-safety) args+=(--without-safety) ;;
        *) printf 'Unknown argument: %s\n' "$argument" >&2; exit 2 ;;
    esac
done

mounts=()
if [[ -n "${EXECUTOR_INPUTS_DIR:-}" ]]; then
    inputs=$(realpath -- "$EXECUTOR_INPUTS_DIR")
    [[ -d "$inputs" && "$inputs" != *','* ]] || { printf 'Invalid input directory\n' >&2; exit 2; }
    mounts+=(--mount "type=bind,src=$inputs,dst=/inputs,readonly")
fi

if [[ -n "${EXECUTOR_POLICY_FILE:-}" ]]; then
    policy=$(realpath -- "$EXECUTOR_POLICY_FILE")
    [[ -f "$policy" && "$policy" != *','* ]] || { printf 'Invalid policy file\n' >&2; exit 2; }
    mounts+=(--mount "type=bind,src=$policy,dst=/etc/executor-policy.json,readonly"
             --env EXECUTOR_POLICY_FILE=/etc/executor-policy.json)
fi

# Не добавлять -t: терминальное форматирование ломает MCP stdio.
# Данные /workspace временные; журнал сохраняется в отдельном Docker volume.
exec docker run --rm -i --init \
    --log-driver local --log-opt max-size=2m --log-opt max-file=3 \
    --read-only \
    --network none \
    --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add KILL \
    --security-opt no-new-privileges:true \
    --pids-limit 128 --memory 256m --memory-swap 256m --cpus 1 \
    --ulimit core=0 --ulimit nofile=256:256 --ulimit fsize=16777216:16777216 \
    --tmpfs /workspace:rw,noexec,nosuid,nodev,size=64m,mode=0770,uid=0,gid=10001 \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=0700 \
    --tmpfs /dev/shm:rw,noexec,nosuid,nodev,size=1m,mode=0700 \
    --mount type=volume,src=spas-executor-logs,dst=/var/log/executor \
    "${mounts[@]}" \
    --entrypoint "${entry[0]}" \
    "$image" "${entry[@]:1}" "${args[@]}"
