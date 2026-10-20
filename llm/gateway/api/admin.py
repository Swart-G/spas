import asyncio
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request


def build_routes(authorize) -> APIRouter:
    routes = APIRouter(prefix="/admin", dependencies=[Depends(authorize)])

    @routes.get("/accounts")
    async def accounts(request: Request) -> dict:
        return {"accounts": request.app.state.auth.accounts()}

    @routes.get("/providers")
    async def providers(request: Request) -> dict:
        backends = list(request.app.state.registry.backends.values())
        health = await asyncio.gather(*(backend.provider.health() for backend in backends))
        return {
            "providers": [
                {
                    "backend": backend.name,
                    "provider": backend.config.provider,
                    "health": status,
                    "capabilities": asdict(backend.provider.capabilities),
                }
                for backend, status in zip(backends, health, strict=True)
            ]
        }

    @routes.get("/routing")
    async def routing(request: Request) -> dict:
        config = request.app.state.registry.config
        return {"routes": config.routes, "fallback_on": config.fallback_on}

    @routes.get("/logs")
    async def logs(request: Request) -> dict:
        return {"logs": request.app.state.audit.recent()}

    @routes.post("/reload")
    async def reload_config(request: Request) -> dict:
        return request.app.state.reload_config()

    return routes
