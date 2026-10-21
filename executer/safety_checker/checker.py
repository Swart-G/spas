"""Conservative, deterministic command policy. Never executes a command."""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from shared.contracts.safety import (
    Decision,
    ExecutionContextPort,
    ExecutorSafetyDecision,
    RiskLevel,
    SafetyResult,
)

# These cannot be overridden by an allow rule or deployment configuration.
_DENIED_EXECUTABLES = frozenset({
    "rm", "rmdir", "unlink", "shred", "dd", "mkfs", "wipefs", "fdisk",
    "shutdown", "reboot", "poweroff", "halt", "mount", "umount",
    "chmod", "chown", "chgrp", "kill", "killall", "pkill", "mv", "cp", "tee",
    "sudo", "su", "doas", "env", "xargs", "find", "eval", "exec", "source",
    "sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "busybox",
    "python", "python3", "perl", "ruby", "node", "php", "lua",
})
_DENIED_DOCKER_ACTIONS = frozenset({
    "rm", "rmi", "prune", "exec", "run", "build", "stop", "kill", "pause",
    "unpause", "update", "cp", "compose", "system", "volume", "network",
    "container", "image", "plugin", "swarm", "stack", "context", "debug",
})
# Keep shell operators and expansions outside the supported argv contract.
_FORBIDDEN = frozenset(";&|<>$`(){}[]*?~#\\")
_ALLOWED_EXECUTABLES = frozenset({"pwd", "uname", "ls", "cat", "docker"})
_EXECUTABLE = re.compile(r"[a-zA-Z0-9_-]+\Z")
_CONTAINER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")
_LS_FLAGS = frozenset({"-l", "-a", "-h", "-la", "-al", "-lh", "-lah", "-alh"})
_UNAME_FLAGS = frozenset({"-a", "-s", "-r", "-m", "-n", "-v"})


@dataclass(frozen=True)
class SafetyPolicy:
    """Trusted deployment configuration; must never come from an agent request.

    Paths are exact canonical absolute names, not directory prefixes or globs.
    File identity/symlinks and access permissions remain Executor responsibilities.
    """

    readable_paths: frozenset[str] = field(default_factory=frozenset)
    containers: frozenset[str] = field(default_factory=frozenset)
    denied_executables: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for name in ("readable_paths", "containers", "denied_executables"):
            value = getattr(self, name)
            if not isinstance(value, (set, frozenset, tuple, list)):
                raise ValueError(f"{name} must be a collection of strings")
            if any(not isinstance(item, str) or not item for item in value):
                raise ValueError(f"{name} must contain non-empty strings")
            object.__setattr__(self, name, frozenset(value))
        for path in self.readable_paths:
            if (
                not path.startswith("/") or path.startswith("//")
                or str(PurePosixPath(path)) != path
                or ".." in PurePosixPath(path).parts
                or any(c in _FORBIDDEN or not 32 <= ord(c) <= 126 for c in path)
            ):
                raise ValueError("readable_paths requires canonical absolute paths")
        if any(not _CONTAINER.fullmatch(name) for name in self.containers):
            raise ValueError("Invalid container name")
        if any(not _EXECUTABLE.fullmatch(name) for name in self.denied_executables):
            raise ValueError("denied_executables requires bare executable names")


class RuleBasedSafetyChecker:
    def __init__(self, policy: SafetyPolicy | None = None) -> None:
        self.policy = policy if policy is not None else SafetyPolicy()
        if not isinstance(self.policy, SafetyPolicy):
            raise TypeError("policy must be a SafetyPolicy")

    async def check(
        self, command: list[str], context: ExecutionContextPort,
    ) -> dict[str, object]:
        """Check argv without parsing or execution; legacy strings are denied.

        Only list[str] is a valid input. Context is metadata, not authorization.
        """
        try:
            values = (context.incident_id, context.reason)
        except AttributeError:
            return {"decision": "DENY", "reason": "Invalid Executor execution context"}
        if any(not isinstance(value, str) or not value.strip() or len(value) > 2000
               for value in values):
            return {"decision": "DENY", "reason": "Invalid Executor execution context"}
        result = self._evaluate(command)
        payload: ExecutorSafetyDecision = {
            "decision": result.decision.value, "reason": result.reason,
        }
        # TypedDict is not a subtype of Executor's mutable dict[str, object].
        return dict(payload)

    def _evaluate(self, command: list[str] | str) -> SafetyResult:
        """Internal policy evaluation; the public interface is async check."""
        if not isinstance(command, list) or not command or len(command) > 4096:
            return SafetyResult(Decision.DENY, RiskLevel.HIGH, "invalid_command",
                                "Expected a non-empty list[str]; command strings are unsupported")
        argv = tuple(command)
        if any(not isinstance(arg, str) or not arg for arg in argv):
            return SafetyResult(Decision.DENY, RiskLevel.HIGH, "invalid_command",
                                "Each argv item must be a non-empty string")
        if sum(len(arg) for arg in argv) + len(argv) - 1 > 4096:
            return SafetyResult(Decision.DENY, RiskLevel.HIGH, "invalid_command",
                                "Command arguments exceed 4096 characters including separators")
        if any(c in _FORBIDDEN or not 32 <= ord(c) <= 126 for arg in argv for c in arg):
            return SafetyResult(Decision.DENY, RiskLevel.HIGH, "shell_syntax",
                                "Shell syntax and control characters are unsupported")

        def result(decision: Decision, risk: RiskLevel, rule: str, reason: str) -> SafetyResult:
            return SafetyResult(decision, risk, rule, reason, argv)

        executable, *args = argv
        basename = PurePosixPath(executable).name
        if basename in _DENIED_EXECUTABLES or basename in self.policy.denied_executables:
            return result(Decision.DENY, RiskLevel.CRITICAL, "denylist",
                          "Executable is explicitly forbidden")
        if not _EXECUTABLE.fullmatch(executable):
            return result(Decision.DENY, RiskLevel.HIGH, "executable_syntax",
                          "Executable paths, assignments and wrappers are unsupported")
        if executable not in _ALLOWED_EXECUTABLES:
            return result(Decision.DENY, RiskLevel.HIGH, "executable_allowlist",
                          "Executable is outside the supported allowlist")
        # Do not guess the subcommand by skipping global flags: some consume values
        # or change the Docker endpoint/configuration. Only explicit forms are supported.
        if executable == "docker" and args and args[0].startswith("-"):
            return result(Decision.DENY, RiskLevel.HIGH, "docker_global_options",
                          "Docker global options are unsupported")
        if executable == "docker" and args and args[0] in _DENIED_DOCKER_ACTIONS:
            return result(Decision.DENY, RiskLevel.CRITICAL, "docker_denylist",
                          "Docker action is explicitly forbidden")

        if executable == "pwd" and not args:
            return result(Decision.ALLOW, RiskLevel.LOW, "read_only", "Working directory query")
        if executable == "uname" and all(arg in _UNAME_FLAGS for arg in args):
            return result(Decision.ALLOW, RiskLevel.LOW, "read_only", "System information query")
        if executable in {"ls", "cat"}:
            paths = args
            if executable == "ls":
                index = 0
                while index < len(args) and args[index] in _LS_FLAGS:
                    index += 1
                paths = args[index:]
            if paths and all(path in self.policy.readable_paths for path in paths):
                return result(Decision.ALLOW, RiskLevel.LOW, "read_paths",
                              "Exact paths are approved for reading")
            return result(Decision.DENY, RiskLevel.HIGH, "read_scope",
                          "Supply only explicitly approved absolute paths and supported flags")
        if executable == "docker":
            if args in (["ps"], ["ps", "-a"], ["ps", "--all"]):
                return result(Decision.ALLOW, RiskLevel.LOW, "docker_ps", "List containers")
            if args and args[0] in {"logs", "restart"}:
                action = args[0]
                # Bound log output; no --follow or arbitrary Docker options.
                valid = (
                    action == "logs" and len(args) == 5 and args[1] == "--tail"
                    and 1 <= len(args[2]) <= 4 and args[2].isascii() and args[2].isdigit()
                    and 1 <= int(args[2]) <= 1000 and args[3] == "--"
                ) or (action == "restart" and len(args) == 3 and args[1] == "--")
                if valid and args[-1] in self.policy.containers:
                    if action == "logs":
                        return result(Decision.ALLOW, RiskLevel.LOW, "docker_logs",
                                      "Bounded logs from an approved container")
                    return result(Decision.REQUIRE_APPROVAL, RiskLevel.MEDIUM, "docker_restart",
                                  "Restart of an approved container requires human approval")
                return result(Decision.DENY, RiskLevel.HIGH, "docker_scope",
                              "Unsupported arguments or container outside the approved scope")
            # Docker also dispatches plugins. Unknown forms must not turn a hard
            # prohibition into an approval request (e.g. by hiding an exec action).
            return result(Decision.DENY, RiskLevel.HIGH, "docker_unsupported",
                          "Docker command or arguments are outside the supported allowlist")
        return result(Decision.DENY, RiskLevel.HIGH, "unsupported_arguments",
                      "Arguments are outside the supported allowlist")
