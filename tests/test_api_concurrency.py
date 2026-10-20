import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx

from llm.gateway.auth.credentials import CredentialStore
from llm.gateway.auth.manager import AuthManager
from llm.gateway.cli import save_config
from llm.gateway.config import BackendConfig, GatewayConfig
from llm.gateway.core.errors import GatewayError


def test_concurrent_profile_creation_has_one_winner(tmp_path):
    barrier = Barrier(2)

    def worker(index):
        store = CredentialStore(tmp_path)
        barrier.wait(timeout=5)

        async def create():
            async with httpx.AsyncClient() as client:
                await AuthManager(store, client).save_api_key(
                    "shared", f"key-{index}", provider="openai_compatible", create_only=True
                )

        try:
            asyncio.run(create())
            return index, "created"
        except GatewayError as error:
            return index, error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker, index) for index in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(code for _, code in results) == ["account_exists", "created"]
    winner = next(index for index, code in results if code == "created")
    assert CredentialStore(tmp_path).get("account:shared")["api_key"] == f"key-{winner}"


def test_concurrent_config_writers_cannot_silently_overwrite(tmp_path):
    path = tmp_path / "gateway.toml"
    initial = GatewayConfig(
        backends={"custom": BackendConfig(provider="openai_api", account="custom", model="model")},
        routes={},
    )
    save_config(initial, path)
    barrier = Barrier(2)

    def worker(index):
        previous = GatewayConfig.load(path)
        data = previous.model_dump()
        data["routes"][f"route-{index}"] = ["custom"]
        updated = GatewayConfig.model_validate(data)
        barrier.wait(timeout=5)
        try:
            save_config(updated, path, expected=previous)
            return index, "saved"
        except GatewayError as error:
            return index, error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker, index) for index in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(code for _, code in results) == ["config_changed", "saved"]
    winner = next(index for index, code in results if code == "saved")
    assert GatewayConfig.load(path).routes == {f"route-{winner}": ["custom"]}
