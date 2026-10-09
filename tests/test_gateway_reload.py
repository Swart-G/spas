import httpx
import pytest

from llm.gateway.cli import save_config
from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.main import GatewayKeys, create_app

KEYS = GatewayKeys(client="client-gateway-key-123456789", admin="admin-gateway-key-123456789")


@pytest.fixture
async def gateway(tmp_path):
    config = GatewayConfig(
        backends={
            "remote": BackendConfig(
                provider="remote_inference",
                account="remote-main",
                model="tiny",
                base_url="http://remote.test",
            )
        },
        routes={"local": ["remote"]},
    )
    path = tmp_path / "gateway.toml"
    save_config(config, path)
    app = create_app(config, state_dir=tmp_path, keys=KEYS, config_path=path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://gateway"
        ) as client:
            yield app, client, config.model_copy(deep=True), path


async def test_reload_requires_admin_key_and_preserves_active_unchanged_backend(gateway):
    app, client, config, path = gateway
    backend = app.state.registry.backends["remote"]
    await backend.semaphore.acquire()
    try:
        config.routes["another-alias"] = ["remote"]
        save_config(config, path)
        denied = await client.post(
            "/admin/reload", headers={"Authorization": "Bearer " + KEYS.client}
        )
        assert denied.status_code == 401
        applied = await client.post(
            "/admin/reload", headers={"Authorization": "Bearer " + KEYS.admin}
        )
        assert applied.status_code == 200
        assert "another-alias" in app.state.registry.config.routes
        assert app.state.registry.backends["remote"] is backend
        assert backend.semaphore.locked()
    finally:
        backend.semaphore.release()


async def test_reload_rejects_modifying_busy_backend_and_invalid_config(gateway):
    app, client, config, path = gateway
    backend = app.state.registry.backends["remote"]
    previous_router = app.state.router
    await backend.semaphore.acquire()
    try:
        config.backends["remote"].model = "another-model"
        save_config(config, path)
        busy = await client.post("/admin/reload", headers={"Authorization": "Bearer " + KEYS.admin})
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "backend_busy"
        assert app.state.router is previous_router
    finally:
        backend.semaphore.release()
    applied = await client.post("/admin/reload", headers={"Authorization": "Bearer " + KEYS.admin})
    assert (
        applied.status_code == 200
        and app.state.registry.backends["remote"].config.model == "another-model"
    )
    previous_router = app.state.router
    path.write_text("invalid TOML and private-token")
    invalid = await client.post("/admin/reload", headers={"Authorization": "Bearer " + KEYS.admin})
    assert invalid.status_code == 400 and invalid.json()["error"]["code"] == "invalid_config"
    assert app.state.router is previous_router
    assert "private-token" not in invalid.text
