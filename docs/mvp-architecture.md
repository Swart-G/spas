# MVP СПАС: решения интеграции

## Источники и выбор реализаций

Интеграционная ветка создана от `origin/main` (`d311eed`). `main` не изменяется.

| Источник | Решение |
| --- | --- |
| `Orchestration/A2A` | Сохранены полный Orchestrator, регистрация агентов, lock на инцидент, раздельные execution/cleanup deadlines, корреляция сообщений, неизменяемые снимки, HTTP-адаптер и регрессионные тесты. |
| `integration/safety-checker-executermcp` | Перенесены Executor MCP, Runner с ограничениями процессов, инструменты диагностики, Rule-Based Checker и Planner. Старый Orchestrator не переносится. |
| `safety-checker/agent-permissions` | Перенесены роли и проверка классов операций, добавлены к Executor с диагностическими инструментами; неизвестные поля инструмента отклоняются до SDK. Сохранены проверки прав и launcher. |
| `safety-checker/-safety-analyzer` | Изучен `AnalyzerSafetyChecker` и его порт `check(argv, context)`. LLM-анализатор остаётся внешней опцией; `CheckedChain` допускает его только после детерминированного ALLOW. Копия самостоятельного приложения и его зависимостей в MVP не нужна. |
| `main` | Сохранён Gateway и его инфраструктура; общие Python-зависимости закреплены в `uv.lock`. |

Прежние демонстрационные агенты и CLI из A2A оставлены для совместимости их тестов.
MVP запускается только через `apps.mvp.service`. Старый Worker, который только
планирует, перенесён в `agents.worker.planning`; соответствующие тесты Planner
сохранены. Его прежний тест передачи плана устаревшему Orchestrator заменён проверкой,
что предложение не содержит подтверждения исполнения. Новые интеграционные тесты
проверяют полный цикл с новым Worker.

## Процессы и протоколы

```text
HTTP :8010 → Orchestrator (Triage локально)
                    ↓ HTTP AgentMessage/AgentResult
             Diagnostic        Worker → Planner (stub или Gateway)
                    ↓ MCP Streamable HTTP ↓
                 Executor: /diagnostic/mcp и /worker/mcp
                    ↓ Permissions → Rule-Based Checker → [optional analyzer]
                    ↓ DemoService: изолированный симулятор
             Diagnostic повторно читает состояние через MCP
                    ↓ Verification → RESOLVED / FAILED
```

В Compose пять постоянно работающих процессов: Orchestrator, Diagnostic, Worker,
Executor, Gateway; одноразовый `gateway-init` подготавливает состояние Gateway.
Triage для единственного сценария детерминированно выбирает `demo-service`.
`PLANNER_MODE=stub` явно означает отсутствие LLM в демонстрации. Gateway всё равно
запускается и проверяется healthcheck. `gateway` mode вызывает существующий
`GatewayPlanner`; ошибки провайдера приводят к FAILED до исполнения.

Агенты не получают Docker socket, host mounts, root и capabilities. Исполнитель
симуляции также непривилегирован. Executor доступен только во внутренней сети.
Только Worker получает credential `/worker/mcp`; Diagnostic получает другой
credential для `/diagnostic/mcp`. Роль связана с экземпляром MCP-сервера, не с
JSON запроса. HTTP агентов доступен в доверенной внутренней сети Compose и требует отдельные
токены Orchestrator: Diagnostic не может вызвать Worker от имени Orchestrator.
TLS и управление ротацией секретов нужны перед внешним размещением.

## Контракты

- `AgentMessage`: `message_id`, `incident_id`, `sender`, `receiver`, `stage`,
  `payload`; `AgentResult` обязан коррелировать оба ID, роль, получателя и этап.
- Diagnosis: непустой `root_cause`, `metrics` со свежими state/logs/availability.
  Тип ответа должен соответствовать инструменту; возраст наблюдений — не более
  30 секунд, допустимое опережение часов — 5 секунд. Неизвестные или противоречивые
  state/availability блокируют и исправление, и положительную Verification.
- Предложение Planner: `summary`, 1–8 текстовых `steps`. Это не команды.
- План исполнения: `proposal`, `runbook=demo-service-v1`, `actions: list[list[str]]`.
  Доверенный runbook выбирает ровно `['spas-demo', 'repair', 'demo-service']`
  для поддержанного диагноза. LLM-текст никогда не разбирается shell/shlex.
- Remediation: `plan`, `execution_results` отдельно от `action_taken`.
  Каждый результат содержит `argv`, `status`, `exit_code`, `safety_checked`,
  `request_id`, ограниченные stdout/stderr. Потерянный ответ записывается `unknown`.
  Сбой сохраняет план и фактические результаты в инциденте и history.
- Executor: `execute_command(command: argv, context: {incident_id, reason})`.
  Дополнительные role/approval/policy поля запрещены. Диагностика использует
  `get_service_state`, `get_service_logs`, `check_service_availability`.
- Verification: строгое `resolved: bool`, непустые `details`, `metrics`.
  Только положительная Verification переводит инцидент в RESOLVED.

## Политика безопасности

Обязательные права проверяются до Checker, проверка Checker ограничена 5 секундами.
Ошибки, некорректные ответы, DENY и REQUIRE_APPROVAL не исполняются.
`demo_simulation=True` — доверенная настройка политики при сборке приложения;
она разрешает только точный argv симулятора. В обычной политике он запрещён.
`docker restart -- demo-service` по-прежнему требует approval, даже в demo-политике.
Доверенный механизм approval пока не реализован: такие планы завершаются FAILED.

`CheckedChain(rules, analyzer)` — точка подключения дополнительного анализатора.
Он не может переопределить DENY/REQUIRE_APPROVAL правил; недоступный подключённый
анализатор блокирует исполнение. При отсутствии анализатора правила продолжают работать.

Для реальных subprocess сохранён отдельный профиль `Run_docker.sh`: read-only root,
network none, ограниченные capabilities контроллера, UID 10001 для команд,
ограничения памяти/PID/вывода, таймаут и обязательная очистка потомков.
Этот Runner отказывает вне защищённого профиля. MVP Compose намеренно использует
симулятор, не открывает привилегированный Runner по сети.

## Неопределённое исполнение и ограничения

Orchestrator блокирует параллельную обработку одного ID. Worker резервирует ID
до планирования; Executor резервирует его после валидации команды и до первого await исполнения. Повторный
запрос, даже с новым message_id, не запускает действие снова. Таймаут/отмена
Remediation переводят инцидент в FAILED. Потеря сетевого ответа не доказывает
отмену удалённой операции и никогда не вызывает автоматического повтора.

Для MVP один план содержит максимум одно изменяющее действие. Executor ledger
использует incident_id, поэтому расширение до нескольких действий требует
стабильного action_id и устойчивого журнала исполнения. Сами результаты Worker
обрабатывает последовательно и останавливается на первом отказе.

Состояние инцидентов, журнал попыток и симулятор — в памяти; один процесс каждого
сервиса, максимум 10 000 инцидентов/попыток, без eviction. Перезапуск теряет данные.
Нельзя использовать рестарт как восстановление незавершённой реальной операции.
Общий рестарт демонстрации создаёт новый симулированный сбой; replay старых ID
после рестарта не поддерживается. Не реализованы распределённая координация,
постоянное хранилище, произвольные runbooks, реальное управление сервисами и UI.

Healthcheck показывает готовность процесса. Порядок старта проверяет зависимости;
их отказ во время работы обрабатывается таймаутами и FAILED, без автоматических
повторов команд. Audit содержит request_id, хеш incident_id, решение безопасности
и результат; stdout/stderr команд не включаются в audit. Логи Compose доступны
через `docker compose ... logs`; файловый журнал симулятора временный.
