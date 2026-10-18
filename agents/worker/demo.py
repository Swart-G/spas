"""Small manual run of the real Worker Agent with a stub or LLM Gateway."""

import argparse
import asyncio
import os
from uuid import uuid4

from shared.contracts.messages import AgentMessage, RoleEnum

from .agent import WorkerAgent
from .planner import GatewayPlanner


async def run(description: str, use_gateway: bool, allow_insecure_http: bool = False) -> None:
    planner = None
    if use_gateway:
        planner = GatewayPlanner(
            base_url=os.environ["SPAS_GATEWAY_URL"],
            model=os.environ["SPAS_WORKER_MODEL"],
            api_key=os.environ["SPAS_GATEWAY_KEY"],
            allow_insecure_http=allow_insecure_http,
        )
    message = AgentMessage(
        incident_id=uuid4(),
        receiver=RoleEnum.REMEDIATION,
        task_description="Составь план устранения инцидента",
        payload={"problem_description": description},
    )
    result = await WorkerAgent(planner).process(message)
    print(result.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Test minimal Worker Agent planning")
    parser.add_argument("description", help="Incident description")
    parser.add_argument("--gateway", action="store_true", help="Use configured LLM Gateway")
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="Allow a non-loopback HTTP Gateway (sends the key and incident without TLS)",
    )
    args = parser.parse_args()
    if args.allow_insecure_http and not args.gateway:
        parser.error("--allow-insecure-http requires --gateway")
    asyncio.run(run(args.description, args.gateway, args.allow_insecure_http))


if __name__ == "__main__":
    main()
