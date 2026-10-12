import httpx
import pytest

from safety_analyzer.config import Settings
from safety_analyzer.contracts import AnalyzeRequest
from safety_analyzer.gateway import LLMGateway
from safety_analyzer.service import Analyzer
from safety_analyzer.stub_gateway import STUB_KEY, create_stub_app


@pytest.mark.parametrize("mode", ["label", "json"])
async def test_local_stub_always_blocks_even_benign_requests(mode):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_stub_app()), base_url="http://stub.test"
    ) as client:
        settings = Settings(
            gateway_base_url="http://stub.test/v1",
            model="stub",
            gateway_api_key=STUB_KEY,
            output_mode=mode,
        )
        result = await Analyzer(LLMGateway(settings, client)).analyze(AnalyzeRequest(text="hello"))
        assert result.status == "ok" and result.decision == "block"
        assert (await client.get("/health")).json()["stub"] is True
