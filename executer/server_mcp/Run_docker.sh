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
    if [[ -d "$inputs/diagnostics" ]]; then
        # The root MCP controller needs group read access to private snapshots.
        # Command subprocesses explicitly drop all supplemental groups.
        [[ ! -L "$inputs/diagnostics" ]] || { printf 'Invalid diagnostic directory\n' >&2; exit 2; }
        operator_uid=$(id -u)
        root_uid=$(stat -c %u -- /)
        ancestor=$(dirname -- "$inputs")
        while :; do
            ancestor_uid=$(stat -c %u -- "$ancestor")
            ancestor_mode=$(stat -c %a -- "$ancestor")
            [[ "$ancestor_mode" =~ ^[0-7]+$ && ( "$ancestor_uid" == "$operator_uid" || "$ancestor_uid" == "$root_uid" ) ]] || {
                printf 'Unsafe diagnostic ancestor ownership\n' >&2; exit 2;
            }
            if (( (8#$ancestor_mode & 022) != 0 )); then
                (( ancestor_uid == root_uid && (8#$ancestor_mode & 01000) != 0 )) || {
                    printf 'Unsafe writable diagnostic ancestor\n' >&2; exit 2;
                }
            fi
            [[ "$ancestor" == / ]] && break
            ancestor=$(dirname -- "$ancestor")
        done
        inputs_uid=$(stat -c %u -- "$inputs")
        inputs_gid=$(stat -c %g -- "$inputs")
        inputs_mode=$(stat -c %a -- "$inputs")
        diagnostics_uid=$(stat -c %u -- "$inputs/diagnostics")
        diagnostics_gid=$(stat -c %g -- "$inputs/diagnostics")
        diagnostics_mode=$(stat -c %a -- "$inputs/diagnostics")
        [[ "$operator_uid" != 10001 \
            && "$inputs_uid" == "$operator_uid" && "$diagnostics_uid" == "$operator_uid" \
            && "$inputs_gid" == "$diagnostics_gid" && "$diagnostics_gid" != 10001 \
            && "$inputs_mode" =~ ^[0-7]+$ && "$diagnostics_mode" =~ ^[0-7]+$ ]] || {
            printf 'Unsafe diagnostic ownership\n' >&2; exit 2;
        }
        (( (8#$inputs_mode & 022) == 0 && (8#$diagnostics_mode & 027) == 0 )) || {
            printf 'Unsafe diagnostic permissions\n' >&2; exit 2;
        }
        mounts+=(--group-add "$diagnostics_gid")
    fi
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
