"""Production orchestration contracts with a bounded, explicit demo runbook."""

import time
from math import isfinite

from agents.worker.planner import RemediationPlan
from executer.server_mcp.Models import DiagnosticResult, ExecutionResult
from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum, StageEnum


class TriageAgent:
    role = RoleEnum.TRIAGE

    async def process(self, message):
        validate(message, self.role, {StageEnum.TRIAGE})
        return reply(message, {"service": "demo-service", "scenario": "isolated-simulation"})


def validate(message, role, stages):
    AgentMessage.model_validate(message.model_dump())
    if (
        message.receiver != role
        or message.sender != RoleEnum.ORCHESTRATOR
        or message.stage not in stages
    ):
        raise ValueError("Invalid agent routing or stage")


def reply(message, data, error=None):
    return AgentResult(
        message_id=message.message_id,
        incident_id=message.incident_id,
        sender=message.receiver,
        stage=message.stage,
        is_success=error is None,
        output_data=data,
        error_message=error,
    )


def context(message):
    return {"incident_id": str(message.incident_id), "reason": message.stage.value}


class DiagnosticAgent:
    role = RoleEnum.DIAGNOSTIC

    def __init__(self, executor):
        self.executor = executor

    async def process(self, message):
        validate(message, self.role, {StageEnum.DIAGNOSIS, StageEnum.VERIFICATION})
        observations = {}
        for tool, expected_kind in (
            ("get_service_state", "state"),
            ("get_service_logs", "logs"),
            ("check_service_availability", "availability"),
        ):
            data = DiagnosticResult.model_validate(
                await self.executor.call(
                    tool,
                    {
                        "service": "demo-service",
                        "context": context(message),
                    },
                )
            )
            now = time.time()
            if (
                data.status != "succeeded"
                or data.service != "demo-service"
                or data.kind != expected_kind
                or data.observed_at is None
                or not isfinite(data.observed_at)
                or not -5 <= now - data.observed_at <= 30
            ):
                return reply(message, {}, "Diagnostics unavailable")
            observations[data.kind] = data.output
        state = observations["state"]
        availability = observations["availability"]
        if (state, availability) not in {
            ("running", "available"),
            ("stopped", "unavailable"),
        }:
            return reply(message, {}, "Unsupported or inconsistent service observations")
        healthy = availability == "available"
        if message.stage == StageEnum.VERIFICATION:
            output = {
                "resolved": healthy,
                "details": "Service available" if healthy else "Service remains unavailable",
                "metrics": observations,
            }
        else:
            output = {
                "root_cause": "demo-service is healthy" if healthy else "demo-service is stopped",
                "metrics": {**observations, "healthy": healthy},
            }
        return reply(message, output)


class WorkerAgent:
    role = RoleEnum.REMEDIATION

    def __init__(self, planner, executor):
        self.planner, self.executor = planner, executor
        self.attempted = set()

    async def process(self, message):
        validate(message, self.role, {StageEnum.REMEDIATION})
        if message.incident_id in self.attempted:
            return reply(message, {}, "Remediation already attempted; outcome may be unknown")
        if len(self.attempted) >= 10000:
            return reply(message, {}, "Worker attempt ledger is full")
        self.attempted.add(message.incident_id)
        cause = message.payload.get("root_cause")
        if cause not in {"demo-service is stopped", "demo-service is healthy"}:
            return reply(message, {}, "No approved runbook for this diagnosis")
        proposal = await self.planner.create_plan(cause)
        proposal = RemediationPlan.model_validate(proposal.model_dump())
        # Natural language is advisory. Only this operator-authored runbook
        # compiles to argv; neither LLM text nor incident description is parsed.
        commands = [] if cause.endswith("healthy") else [["spas-demo", "repair", "demo-service"]]
        plan = {
            "proposal": proposal.model_dump(),
            "actions": commands,
            "runbook": "demo-service-v1",
        }
        results = []
        for argv in commands:
            try:
                result = ExecutionResult.model_validate(
                    await self.executor.call(
                        "execute_command",
                        {
                            "command": argv,
                            "context": context(message),
                        },
                    ),
                    strict=True,
                )
            except Exception:
                results.append({"status": "unknown", "argv": argv})
                return reply(
                    message,
                    {"plan": plan, "execution_results": results},
                    "Execution outcome unknown; automatic retry is forbidden",
                )
            results.append({"argv": argv, **result.model_dump()})
            if result.status != "succeeded" or not result.safety_checked or result.exit_code != 0:
                return reply(
                    message,
                    {"plan": plan, "execution_results": results},
                    "Execution was not confirmed successful",
                )
        return reply(
            message,
            {
                "plan": plan,
                "execution_results": results,
                "action_taken": "Simulated repair executed" if commands else "No change required",
            },
        )
