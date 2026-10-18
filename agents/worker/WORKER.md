# Минимальный Worker Agent

На этом этапе Worker получает описание проблемы и **составляет план**. Он не
выполняет команды и не обращается к Executor MCP. Выполнение и повторная
диагностика относятся к следующим этапам проекта.

## Интерфейс между агентами

`WorkerAgent.process(message)` принимает общий `AgentMessage` с
`receiver=RoleEnum.REMEDIATION` и возвращает `AgentResult` с теми же
`message_id` и `incident_id`, `sender=RoleEnum.REMEDIATION` и
`receiver=RoleEnum.ORCHESTRATOR`. Описание проблемы берётся из
`payload["problem_description"]`, затем из `payload["text"]`. Если оба поля
отсутствуют, используется `task_description`. Успешный результат содержит
`output_data["plan"]` с `summary` и списком `steps`. Это текстовые шаги,
они не являются аргументами для запуска команды.

```python
import asyncio
from uuid import uuid4
from agents.worker import WorkerAgent
from shared.contracts.messages import AgentMessage, RoleEnum

async def main():
    message = AgentMessage(
        incident_id=uuid4(),
        receiver=RoleEnum.REMEDIATION,
        task_description="Составь план устранения инцидента",
        payload={"problem_description": "PostgreSQL connection timeout"},
    )
    result = await WorkerAgent().process(message)  # встроенная заглушка
    print(result.model_dump_json(indent=2))

asyncio.run(main())
```

Для замены заглушки достаточно передать объект с асинхронным методом
`create_plan(problem) -> RemediationPlan`. Встроенный `GatewayPlanner`
использует существующий маршрут LLM Gateway `/v1/chat/completions` и ключ
клиента Gateway. Он просит модель вернуть JSON и проверяет его структуру.
Ошибки планировщика возвращаются как `AgentResult(is_success=False)`.

## Ручной прогон

Из корня репозитория после установки `requirements.txt`:

```sh
python -m agents.worker.demo "PostgreSQL connection timeout"
```

Для подключённого LLM Gateway:

```sh
export SPAS_GATEWAY_URL=http://127.0.0.1:8000
export SPAS_WORKER_MODEL=codex-default
export SPAS_GATEWAY_KEY='ключ клиента Gateway'
python -m agents.worker.demo --gateway "PostgreSQL connection timeout"
```

`SPAS_WORKER_MODEL` — alias маршрута или имя модели, настроенное в Gateway.
Адрес Gateway и ключ задаются оператором, в репозиторий их не добавляют.

Проверка: `python -m unittest discover -s tests -p 'test_worker_agent.py' -v`.

Текущий Orchestrator пока вызывает только Triage. Тестовый прогон объединяет
его реальный этап Triage с реальным Worker и заглушкой источника плана;
автоматическое переключение Orchestrator на Worker пока не реализовано.
