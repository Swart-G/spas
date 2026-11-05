"""Подключение готового checker. Политика задаётся оператором, не Worker."""

import json
import os
from pathlib import Path, PurePosixPath

from executer.safety_checker import RuleBasedSafetyChecker, SafetyPolicy


def load_checker() -> RuleBasedSafetyChecker:
    path = os.environ.get("EXECUTOR_POLICY_FILE")
    values = {"readable_paths": ["/workspace", "/inputs"]}
    if path:
        with Path(path).open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("Executor policy is too large")
        values = json.loads(raw)
        if not isinstance(values, dict) or set(values) - {"readable_paths", "containers", "denied_executables"}:
            raise ValueError("Invalid Executor policy fields")
    policy = SafetyPolicy(**values)
    for name in policy.readable_paths:
        parts = PurePosixPath(name).parts
        if name != "/workspace" and not (len(parts) >= 2 and parts[1] == "inputs"):
            raise ValueError("Reading is scoped to /workspace listing and /inputs")
    # Этот профиль не предоставляет Docker socket или CLI для управления хостом.
    # Сам RuleBasedSafetyChecker поддерживает docker, но доступ здесь отключён.
    return RuleBasedSafetyChecker(SafetyPolicy(
        readable_paths=policy.readable_paths,
        containers=policy.containers,
        denied_executables=policy.denied_executables | {"docker"},
    ))
