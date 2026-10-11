"""Conservative, deterministic command policy. Never executes a command."""

import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from shared.contracts.safety import CommandRequest, Decision, RiskLevel, SafetyResult

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
# Intentionally reject even quoted shell operators, escapes and expansions.
_FORBIDDEN = frozenset(";&|<>$`(){}[]*?~#\\")
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

    def check(self, request: CommandRequest) -> SafetyResult:
        """Validate one command and return a decision without side effects."""
        if not isinstance(request, CommandRequest) or not isinstance(request.command, str):
            return SafetyResult(Decision.DENY, RiskLevel.HIGH, "invalid_request",
                                "Expected CommandRequest with a string command")
        command = request.command
        if len(command) > 4096 or not command.strip():
            return SafetyResult(Decision.DENY, RiskLevel.HIGH, "invalid_command",
                                "Command is empty or exceeds 4096 characters")
        if any(c in _FORBIDDEN or not 32 <= ord(c) <= 126 for c in command):
            return SafetyResult(Decision.DENY, RiskLevel.HIGH, "shell_syntax",
                                "Shell syntax, expansions and control characters are unsupported")
        try:
            argv = tuple(shlex.split(command, comments=False, posix=True))
        except ValueError:
            return SafetyResult(Decision.DENY, RiskLevel.HIGH, "invalid_command",
                                "Unbalanced command quoting")
        if not argv or any(not arg for arg in argv):
            return SafetyResult(Decision.DENY, RiskLevel.HIGH, "invalid_command",
                                "Empty command or argument")

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
        return result(Decision.REQUIRE_APPROVAL, RiskLevel.HIGH, "unknown_command",
                      "No allow rule matches this exact command and its arguments")
