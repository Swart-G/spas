"""MCP client: literal structured arguments and no automatic retries."""

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class ExecutorClient:
    def __init__(self, url, token, *, timeout=8):
        self.url, self.token, self.timeout = url, token, timeout

    async def call(self, name, arguments):
        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer " + self.token},
            timeout=self.timeout,
            trust_env=False,
        ) as http:
            async with streamable_http_client(self.url, http_client=http) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=self.timeout,
                ) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    data = result.structured_content
                    if not isinstance(data, dict):
                        raise ValueError("Executor returned no structured result")
                    return data
