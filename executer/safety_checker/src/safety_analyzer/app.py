import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from safety_analyzer.command_contracts import CommandAnalysisRequest, CommandAnalysisResponse
from safety_analyzer.commands import CommandAnalyzer, configured_rules
from safety_analyzer.config import Settings
from safety_analyzer.contracts import AnalyzeRequest, AnalyzeResponse
from safety_analyzer.gateway import LLMGateway
from safety_analyzer.service import Analyzer


class BodyLimit:
    """Bound incoming bytes even with chunked transfer and no Content-Length."""

    def __init__(self, app: ASGIApp, limit: int = 262144):
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.limit:
                await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(
    settings: Settings | None = None, client: httpx.AsyncClient | None = None
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        # Connect directly to the configured Gateway, independent of system proxy settings.
        gateway_client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        try:
            app.state.analyzer = Analyzer(LLMGateway(settings, gateway_client))
            app.state.command_analyzer = CommandAnalyzer(
                app.state.analyzer.gateway, configured_rules(settings)
            )
            yield
        finally:
            if client is None:
                await gateway_client.aclose()

    app = FastAPI(title="Safety Analyzer", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BodyLimit)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError):
        # Never echo candidate text, context, or arbitrary validation context.
        return JSONResponse(
            status_code=422,
            content={"detail": [{"loc": e["loc"], "type": e["type"]} for e in exc.errors()]},
        )

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        # Configuration readiness only; upstream availability needs a functional probe.
        return JSONResponse(
            {"status": "configured" if settings.configured else "not_configured"},
            status_code=200 if settings.configured else 503,
        )

    @app.post(
        "/v1/analyze",
        response_model=AnalyzeResponse,
        responses={
            503: {"model": AnalyzeResponse, "description": "Classification failed; block"},
            413: {"description": "Request body exceeds 262144 bytes"},
        },
    )
    async def analyze(request: AnalyzeRequest):
        if request.target == "command":
            return JSONResponse(
                {"detail": "Use /v1/commands/analyze with literal argv and incident context"}, 422
            )
        response = await app.state.analyzer.analyze(request)
        return JSONResponse(
            response.model_dump(mode="json"), status_code=200 if response.status == "ok" else 503
        )

    @app.post(
        "/v1/commands/analyze",
        response_model=CommandAnalysisResponse,
        responses={503: {"model": CommandAnalysisResponse, "description": "Fail-closed DENY"}},
    )
    async def analyze_command(request: CommandAnalysisRequest):
        response = await app.state.command_analyzer.analyze(request)
        return JSONResponse(
            response.model_dump(mode="json"), status_code=200 if response.status == "ok" else 503
        )

    return app
