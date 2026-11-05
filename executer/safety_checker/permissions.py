"""Agent permissions are assigned by the application, never by command context."""

from enum import StrEnum


class AgentRole(StrEnum):
    DIAGNOSTIC = "diagnostic"
    WORKER = "worker"


def permits_command(role: AgentRole, argv: tuple[str, ...]) -> bool:
    """Authorize the operation class; Safety Checker still validates all arguments.

    Diagnostic can only collect information. Worker may propose changes, but
    this permission grants neither an ALLOW decision nor human approval.
    """
    if not isinstance(role, AgentRole) or not argv:
        return False
    if role is AgentRole.WORKER:
        return True
    return argv[0] in {"pwd", "uname", "ls", "cat"} or (
        argv[0] == "docker" and len(argv) > 1 and argv[1] in {"ps", "logs"}
    )
