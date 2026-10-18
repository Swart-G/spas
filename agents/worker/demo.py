"""Small manual run of the real Worker Agent with a stub or LLM Gateway."""

import argparse
import asyncio
import os
from uuid import uuid4

from shared.contracts.messages import AgentMessage, RoleEnum

from .agent import WorkerAgent
from .planner import GatewayPlanner


async def run(description: str, use_gateway: bool) -> None:
    planner = None
    if use_gateway:
        planner = GatewayPlanner(
            base_url=os.environ["SPAS_GATEWAY_URL"],
            model=os.environ["SPAS_WORKER_MODEL"],
            api_key=os.environ["SPAS_GATEWAY_KEY"],
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
    args = parser.parse_args()
    asyncio.run(run(args.description, args.gateway))


if __name__ == "__main__":
    main()
