"""Run an incident using explicitly selected stubs or an application's agent factory."""

import argparse
import asyncio
from collections.abc import Mapping
from importlib import import_module
from inspect import isawaitable

from core.incidents.models import IncidentStatus
from core.orchestrator.engine import Orchestrator
from shared.contracts import Agent, RoleEnum


async def load_agents(factory_path: str) -> Mapping[RoleEnum, Agent]:
    module_name, separator, name = factory_path.partition(":")
    if not separator or not module_name or not name or ":" in name:
        raise ValueError("Agent factory must be specified as module:function")
    factory = getattr(import_module(module_name), name)
    if not callable(factory):
        raise TypeError("Agent factory must be callable")
    agents = factory()
    if isawaitable(agents):
        agents = await agents
    if not isinstance(agents, Mapping):
        raise TypeError("Agent factory must return a role-to-agent mapping")
    return agents


async def run(args: argparse.Namespace) -> int:
    if args.stub:
        from apps.agent_setup import stub_agents

        agents = stub_agents()
    else:
        agents = await load_agents(args.agent_factory)
    engine = Orchestrator(
        agents=agents, agent_timeout=args.agent_timeout, cleanup_timeout=args.cleanup_timeout
    )
    incident = await engine.create_incident(args.title, args.description)
    await engine.process_incident(incident.incident_id)
    print(incident.model_dump_json(indent=2))
    return 0 if incident.status == IncidentStatus.RESOLVED else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stub", action="store_true", help="Use the bundled simulation agents")
    mode.add_argument(
        "--agent-factory", metavar="MODULE:FUNCTION", help="Load trusted application code"
    )
    parser.add_argument("--title", default="Alert: DB_TIMEOUT")
    parser.add_argument("--description", default="Database connections are dropping")
    parser.add_argument("--agent-timeout", type=float, default=10.0)
    parser.add_argument("--cleanup-timeout", type=float, default=10.0)
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
