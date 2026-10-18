import asyncio
from dataclasses import asdict, dataclass, field

from ..config import BackendConfig, GatewayConfig
from ..providers.base import LLMProvider
from .errors import GatewayError
from .models import Capabilities


@dataclass
class Backend:
    name: str
    config: BackendConfig
    provider: LLMProvider
    semaphore: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.config.max_concurrency)


class Registry:
    def __init__(self, config: GatewayConfig, providers: dict[str, LLMProvider]) -> None:
        self.config = config
        self.backends = {
            name: Backend(name, backend, providers[name])
            for name, backend in config.backends.items()
        }

    def resolve(self, alias: str) -> list[Backend]:
        names = self.config.routes.get(alias)
        if names is None:
            raise GatewayError(
                "Логическое имя модели не настроено.",
                code="model_not_found",
                status=404,
                param="model",
            )
        return [self.backends[name] for name in names]

    def capabilities(self, alias: str) -> Capabilities:
        backends = self.resolve(alias)
        return Capabilities(
            **{
                name: all(getattr(backend.provider.capabilities, name) for backend in backends)
                for name in asdict(Capabilities())
            }
        )

    def models(self) -> list[dict]:
        return [
            {
                "id": alias,
                "object": "model",
                "created": 0,
                "owned_by": "spas",
                "capabilities": asdict(self.capabilities(alias)),
            }
            for alias in self.config.routes
        ]
