"""Worker planning and handoff tests, runnable with the standard library."""

import asyncio
import gzip
import json
import unittest
from uuid import uuid4

import httpx

from agents.base import BaseSlaveAgent
from agents.worker import GatewayPlanner, StubPlanner, WorkerAgent
from agents.worker.planner import MAX_GATEWAY_RESPONSE_BYTES, RemediationPlan
from core.incidents.models import IncidentStatus
from core.orchestrator.engine import Orchestrator
from shared.contracts.messages import AgentMessage, AgentResult, RoleEnum


def worker_message(problem: str = "PostgreSQL connection timeout") -> AgentMessage:
    return AgentMessage(
        incident_id=uuid4(),
        receiver=RoleEnum.REMEDIATION,
        task_description="Составь план устранения инцидента",
        payload={"problem_description": problem},
    )


async def triage_result(message: AgentMessage) -> AgentResult:
    return AgentResult(
        message_id=message.message_id,
        incident_id=message.incident_id,
        sender=RoleEnum.TRIAGE,
        is_success=True,
        output_data={"severity": "CRITICAL"},
    )


class TrackedResponseStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.chunks_read = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.chunks_read += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_with_stub_returns_correlated_plan(self):
        message = worker_message()
        worker = WorkerAgent(StubPlanner())
        self.assertIsInstance(worker, BaseSlaveAgent)
        result = await worker.process(message)

        self.assertIsInstance(result, AgentResult)
        self.assertEqual(result.message_id, message.message_id)
        self.assertEqual(result.incident_id, message.incident_id)
        self.assertEqual(result.sender, RoleEnum.REMEDIATION)
        self.assertEqual(result.receiver, RoleEnum.ORCHESTRATOR)
        self.assertTrue(result.is_success)
        self.assertEqual(
            result.output_data["plan"]["summary"], "PostgreSQL connection timeout"
        )
        self.assertEqual(len(result.output_data["plan"]["steps"]), 3)
        self.assertIn("базы данных", result.output_data["plan"]["steps"][0])
        with self.assertRaises(TypeError):
            result.output_data["plan"]["steps"].append("run arbitrary command")

    async def test_real_orchestrator_triage_then_real_worker_handoff(self):
        orchestrator = Orchestrator(agent_timeout=3)
        incident = await orchestrator.create_incident(
            "DB alert", "PostgreSQL connection timeout"
        )
        # The real Orchestrator currently supports Triage, using a stub agent.
        orchestrator.agents[RoleEnum.TRIAGE].process = triage_result
        await orchestrator.execute_next_step(incident.incident_id)
        self.assertEqual(incident.status, IncidentStatus.INVESTIGATING)
        self.assertEqual(len(incident.history), 1)

        message = AgentMessage(
            incident_id=incident.incident_id,
            receiver=RoleEnum.REMEDIATION,
            task_description="Составь план устранения инцидента",
            payload={"problem_description": incident.description, "triage": incident.metrics_log},
        )
        result = await WorkerAgent().process(message)
        self.assertTrue(result.is_success)
        self.assertEqual(result.message_id, message.message_id)
        self.assertEqual(result.incident_id, incident.incident_id)
        self.assertTrue(result.output_data["plan"]["steps"])
        accepted = orchestrator._validate_result(message, result)
        orchestrator._handle_agent_result(incident, accepted)
        self.assertEqual(incident.history[-1], result)

    async def test_gateway_planner_calls_existing_chat_contract(self):
        requests = []

        def gateway(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            plan = {"summary": "Database timeout", "steps": ["Check connections", "Check logs"]}
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "stop", "message": {"content": json.dumps(plan)},
            }]})

        planner = GatewayPlanner(
            base_url="https://gateway.example/v1",
            model="worker-default",
            api_key="test-key",
            transport=httpx.MockTransport(gateway),
        )
        result = await WorkerAgent(planner).process(worker_message())

        self.assertTrue(result.is_success)
        self.assertEqual(
            result.output_data["plan"]["steps"], ["Check connections", "Check logs"]
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "https://gateway.example/v1/chat/completions")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-key")
        self.assertEqual(requests[0].headers["Accept-Encoding"], "identity")
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], "worker-default")
        self.assertEqual(body["messages"][-1]["content"], "PostgreSQL connection timeout")

    async def test_gateway_failure_is_a_correlated_agent_result(self):
        planner = GatewayPlanner(
            base_url="https://gateway.example",
            model="worker-default",
            api_key="test-key",
            transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        )
        message = worker_message()
        result = await WorkerAgent(planner).process(message)
        self.assertFalse(result.is_success)
        self.assertEqual(result.message_id, message.message_id)
        self.assertEqual(result.incident_id, message.incident_id)
        self.assertEqual(result.output_data, {})
        self.assertEqual(result.error_message, "Worker could not create a valid plan")

    async def test_gateway_accepts_only_stop_even_with_valid_plan(self):
        complete_looking_plan = json.dumps({
            "summary": "Database timeout", "steps": ["Check connections"]
        })
        reasons = [
            "stop", "length", "content_filter", "tool_calls", "function_call",
            "unknown", "STOP", "", None, False, 0, ["stop"], {},
        ]
        for fields in [{}, *({"finish_reason": reason} for reason in reasons)]:
            with self.subTest(fields=fields):
                choice = {"message": {"content": complete_looking_plan}, **fields}
                response = httpx.Response(200, json={"choices": [choice]})
                planner = GatewayPlanner(
                    base_url="https://gateway.example",
                    model="worker-default",
                    api_key="test-key",
                    transport=httpx.MockTransport(lambda request: response),
                )
                message = worker_message()
                result = await WorkerAgent(planner).process(message)
                self.assertEqual(result.message_id, message.message_id)
                self.assertEqual(result.incident_id, message.incident_id)
                self.assertEqual(result.is_success, fields.get("finish_reason") == "stop")
                if result.is_success:
                    self.assertEqual(result.output_data["plan"]["steps"], ["Check connections"])
                else:
                    self.assertEqual(result.output_data, {})
                    self.assertEqual(result.error_message, "Worker could not create a valid plan")

    async def test_gateway_accepts_maximum_plan_with_json_escaping(self):
        # Exercise both JSON layers: the model's plan and Gateway's envelope.
        for character in ("Я", "😀", '"', "\\", "\x00"):
            plan = {"summary": character * 1000, "steps": [character * 500] * 8}
            RemediationPlan.model_validate(plan)
            for inner_ascii in (False, True):
                for outer_ascii in (False, True):
                    with self.subTest(
                        character=character, inner_ascii=inner_ascii, outer_ascii=outer_ascii
                    ):
                        content = json.dumps(plan, ensure_ascii=inner_ascii, indent=2)
                        envelope = {
                            "id": "chatcmpl-worker-test",
                            "created": 1792411200,
                            "model": "worker-default",
                            "object": "chat.completion",
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }],
                            "usage": {"prompt_tokens": 100, "completion_tokens": 10000},
                        }
                        body = json.dumps(envelope, ensure_ascii=outer_ascii).encode("utf-8")
                        response = httpx.Response(200, content=body)
                        planner = GatewayPlanner(
                            base_url="https://gateway.example",
                            model="worker-default",
                            api_key="test-key",
                            transport=httpx.MockTransport(lambda request: response),
                        )
                        result = await WorkerAgent(planner).process(worker_message())
                        self.assertTrue(result.is_success)
                        self.assertEqual(result.model_dump()["output_data"]["plan"], plan)

    async def test_gateway_bounds_streamed_response_and_closes_it(self):
        plan = {"summary": "Database timeout", "steps": ["Check connections"]}
        body = json.dumps({"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(plan)},
        }]}).encode("utf-8")
        for extra in (-1, 0, 1):
            for content_length in (None, "1"):
                with self.subTest(extra=extra, content_length=content_length):
                    # Padding keeps the complete response valid JSON: rejection
                    # must come from its byte size, not a parser/schema error.
                    padded = body + b" " * (MAX_GATEWAY_RESPONSE_BYTES - len(body) + extra)
                    midpoint = len(padded) // 2
                    chunks = [padded[:midpoint], padded[midpoint:]]
                    if extra > 0:
                        chunks.append(b"must never be consumed")
                    stream = TrackedResponseStream(chunks)
                    headers = {} if content_length is None else {"Content-Length": content_length}
                    response = httpx.Response(200, stream=stream, headers=headers)
                    planner = GatewayPlanner(
                        base_url="https://gateway.example",
                        model="worker-default",
                        api_key="test-key",
                        transport=httpx.MockTransport(lambda request: response),
                    )
                    if extra > 0:
                        with self.assertRaisesRegex(ValueError, "Gateway response is too large"):
                            await planner.create_plan("Database timeout")
                    else:
                        result = await planner.create_plan("Database timeout")
                        self.assertEqual(result.model_dump(), plan)
                    self.assertEqual(stream.chunks_read, 2)
                    self.assertTrue(stream.closed)

    async def test_gateway_rejects_compressed_response_before_decoding(self):
        plan = {"summary": "Database timeout", "steps": ["Check connections"]}
        body = json.dumps({"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(plan)},
        }]}).encode("utf-8")
        compressed = gzip.compress(body)
        nested = gzip.compress(gzip.compress(body + b" " * (MAX_GATEWAY_RESPONSE_BYTES * 8)))
        for encoding, payload in (
            ("gzip", compressed),
            ("GZIP", compressed),
            ("gzip, gzip", nested),
            ("identity, gzip", compressed),
            ("br", b"compressed"),
        ):
            with self.subTest(encoding=encoding):
                stream = TrackedResponseStream([payload])
                response = httpx.Response(
                    200, stream=stream, headers={"Content-Encoding": encoding}
                )
                planner = GatewayPlanner(
                    base_url="https://gateway.example",
                    model="worker-default",
                    api_key="test-key",
                    transport=httpx.MockTransport(lambda request: response),
                )
                with self.assertRaisesRegex(ValueError, "must be uncompressed"):
                    await planner.create_plan("Database timeout")
                self.assertEqual(stream.chunks_read, 0)
                self.assertTrue(stream.closed)

        # Explicit identity is still a normal, bounded Gateway response.
        planner = GatewayPlanner(
            base_url="https://gateway.example",
            model="worker-default",
            api_key="test-key",
            transport=httpx.MockTransport(lambda request: httpx.Response(
                200, content=body, headers={"Content-Encoding": "IDENTITY"}
            )),
        )
        self.assertEqual((await planner.create_plan("Database timeout")).model_dump(), plan)

    async def test_gateway_still_rejects_plans_beyond_schema_limits(self):
        for plan in (
            {"summary": "Я" * 1001, "steps": ["Check connections"]},
            {"summary": "Database timeout", "steps": ["Я" * 501]},
            {"summary": "Database timeout", "steps": ["Check connections"] * 9},
        ):
            with self.subTest(summary_length=len(plan["summary"]), steps=len(plan["steps"])):
                response = httpx.Response(200, json={"choices": [{
                    "finish_reason": "stop", "message": {"content": json.dumps(plan)},
                }]})
                planner = GatewayPlanner(
                    base_url="https://gateway.example",
                    model="worker-default",
                    api_key="test-key",
                    transport=httpx.MockTransport(lambda request: response),
                )
                result = await WorkerAgent(planner).process(worker_message())
                self.assertFalse(result.is_success)
                self.assertEqual(result.output_data, {})

    async def test_gateway_rejects_remote_plain_http_without_explicit_opt_in(self):
        for base_url in (
            "http://gateway.example:8008",
            "http://localhost.evil.example:8008",
            "http://127.0.0.1.evil.example:8008",
        ):
            with self.subTest(base_url=base_url), self.assertRaisesRegex(ValueError, "explicit opt-in"):
                GatewayPlanner(base_url=base_url, model="worker-default", api_key="test-key")
        with self.assertRaisesRegex(ValueError, "boolean"):
            GatewayPlanner(
                base_url="http://gateway.example:8008",
                model="worker-default",
                api_key="test-key",
                allow_insecure_http="false",
            )
        for base_url in ("https://gateway.example?", "https://gateway.example#"):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                GatewayPlanner(base_url=base_url, model="worker-default", api_key="test-key")
        for base_url in ("http://127.0.0.1:8008", "http://localhost:8008", "http://[::1]:8008"):
            GatewayPlanner(base_url=base_url, model="worker-default", api_key="test-key")

        requests = []

        def gateway(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            plan = {"summary": "Check", "steps": ["Inspect service"]}
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "stop", "message": {"content": json.dumps(plan)},
            }]})

        planner = GatewayPlanner(
            base_url="http://gateway:8008",
            model="worker-default",
            api_key="test-key",
            allow_insecure_http=True,
            transport=httpx.MockTransport(gateway),
        )
        result = await WorkerAgent(planner).process(worker_message())
        self.assertTrue(result.is_success)
        self.assertEqual(str(requests[0].url), "http://gateway:8008/v1/chat/completions")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-key")

    async def test_worker_rejects_message_from_other_agent(self):
        message = worker_message().model_copy(update={"sender": RoleEnum.TRIAGE})
        with self.assertRaisesRegex(ValueError, "only from Orchestrator"):
            await WorkerAgent().process(message)

    async def test_invalid_plan_is_not_passed_to_other_agents(self):
        class InvalidPlanner:
            async def create_plan(self, problem):
                return {"summary": " ", "steps": [" "]}

        result = await WorkerAgent(InvalidPlanner()).process(worker_message())
        self.assertFalse(result.is_success)
        self.assertEqual(result.output_data, {})

        class BypassedValidationPlanner:
            async def create_plan(self, problem):
                return RemediationPlan(summary="ok", steps=["inspect"]).model_copy(
                    update={"steps": ["x" * 501]}
                )

        result = await WorkerAgent(BypassedValidationPlanner()).process(worker_message())
        self.assertFalse(result.is_success)
        self.assertEqual(result.output_data, {})

    async def test_wrong_receiver_and_cancellation(self):
        wrong_receiver = AgentMessage(
            incident_id=uuid4(), receiver=RoleEnum.TRIAGE, task_description="Problem"
        )
        with self.assertRaisesRegex(ValueError, "not addressed"):
            await WorkerAgent().process(wrong_receiver)

        class CancelledPlanner:
            async def create_plan(self, problem):
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await WorkerAgent(CancelledPlanner()).process(worker_message())

    def test_plan_model_rejects_extra_fields_and_long_steps(self):
        with self.assertRaises(ValueError):
            RemediationPlan(summary="x", steps=["x"], command="rm -rf /")
        with self.assertRaises(ValueError):
            RemediationPlan(summary="x", steps=["x" * 501])


if __name__ == "__main__":
    unittest.main()
