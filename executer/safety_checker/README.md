# Safety Analyzer

Модуль анализа argv и полного предоставленного контекста для СПАС. Обязательный
Rule-Based Checker выполняется перед LLM; запреты и запросы подтверждения модель
переопределить не может. Выбрана `inception/mercury-decide:free` через OpenRouter.ai.
Для локальной разработки есть заглушка; качество реальной модели ещё не измерялось.

Рабочий контракт команд: **2.0 / command-1**, [описание](docs/contracts.md).
[Интеграция с Executor MCP](docs/executor.md) включает host-side `CheckedExecutor`,
который сохраняет изоляцию контейнера и повторную проверку правил на стороне MCP.
[Варианты модели](docs/models.md) и пороги качества требуют проверки владельцем политики.

Реализованы FastAPI API, строгая проверка ответа модели, fail-closed обработка ошибок,
адаптер LLM Gateway, JSON-логирование без содержимого запросов, датасет, offline и
опциональные live-тесты, оценка стабильности/false allow и контейнеризация.

Совместимость проверяется с [SPAS Gateway](https://github.com/Swart-G/spas/tree/d311eedf6e75d1adcbe6668101e6bc69a1ed4734)
через его настоящий HTTP API и RemoteInference/OpenAI-compatible адаптеры с подменённым inference.
Gateway уже поддерживает текст; изменений в SPAS для текущего контракта не требуется.
Адаптер не запрашивает неподдерживаемый `json_schema`.

## Быстрый запуск с заглушкой

Python 3.12+, uv 0.12.23. Из корня SPAS перейдите в каталог компонента:

```bash
cd executer/safety_checker
```

Все команды ниже выполняются из этого каталога:

```bash
uv sync --frozen --group integration
uv run --frozen --group integration pytest -q
uv run --frozen --group integration ruff check src tests scripts
uv run --frozen --group integration python scripts/dev.py
```

`dev.py` запускает заглушку на порту 8008 и Analyzer на 8000, только на loopback,
проверяет реальный HTTP-ответ `/v1/analyze` и остаётся запущенным до Ctrl+C.
Секреты для него не нужны: используется публичный ключ `local-stub-only`.
Заглушка **всегда возвращает `unsafe`**, включая безопасный текст. Это позволяет
проверить интеграцию, но не качество классификации. Внешний Gateway не вызывается.

```bash
curl -sS http://127.0.0.1:8000/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{"text":"Привет","target":"request"}'
```

Для автоматической проверки запуска и остановки:
`uv run --frozen --group integration python scripts/dev.py --smoke`.

## Подключение выбранной модели

См. [контракты](docs/contracts.md) и [Gateway](docs/gateway.md). Настройки читаются из
окружения; `.env.example` — шаблон, автоматической загрузки `.env` нет.
Храните настоящий client key Gateway в секретах среды. Не используйте admin key
или ключ провайдера модели как ключ Analyzer → Gateway.

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `SAFETY_GATEWAY_BASE_URL` | Полный адрес Gateway с `/v1` | не задан |
| `SAFETY_GATEWAY_API_KEY` | Bearer client key Gateway | не задан |
| `SAFETY_MODEL` | Логический маршрут Gateway | не задан |
| `SAFETY_OUTPUT_MODE` | `label` или `json` | `label` |
| `SAFETY_TIMEOUT_SECONDS` | Общий deadline классификации | `10` |
| `SAFETY_MAX_TOKENS` | Максимум выходных токенов | `128` |
| `SAFETY_COMMAND_API` | `chat_completions` либо native `decisions` (Nimble) | `chat_completions` |
| `SAFETY_COMMAND_OUTPUT_MODE` | JSON-решение или native label для команд | `json` |
| `SAFETY_COMMAND_MAX_TOKENS` | Лимит ответа анализа команды | `512` |
| `SAFETY_COMMAND_TIMEOUT_SECONDS` | Общий бюджет правил + модели | `4` |
| `SAFETY_RULE_TIMEOUT_SECONDS` | Бюджет обязательного Checker/provider | `0.5` |
| `SAFETY_READABLE_PATHS` | JSON-массив точных разрешённых путей | `[]` |
| `SAFETY_CONTAINERS` | JSON-массив разрешённых контейнеров | `[]` |
| `SAFETY_DENIED_EXECUTABLES` | Дополнительные запреты executable | `[]` |

Настройки scopes принадлежат оператору; значения из HTTP/context не расширяют их.
Пример контекстного анализа (dev-заглушка вернёт DENY/model):

```bash
curl -sS http://127.0.0.1:8000/v1/commands/analyze \
  -H 'Content-Type: application/json' \
  -d '{"command":["pwd"],"context":{"incident_id":"incident-1","reason":"Проверить рабочий каталог","constraints":["Только чтение"]}}'
```

```bash
uv run --frozen uvicorn safety_analyzer.app:create_app --factory --host 127.0.0.1 --port 8000
```

`/health/live` проверяет жизнь процесса. `/health/ready` проверяет наличие конфигурации,
без вызова модели; для доступности inference нужен функциональный запрос.
Без настроенного Gateway анализ возвращает HTTP 503 с `decision=block`, `verdict=unknown`.
HTTP-клиенты сервиса и оценки подключаются напрямую к Gateway (`trust_env=False`)
и игнорируют системные proxy-переменные, включая SOCKS. Наличие `socksio` не требуется.
API и заглушка предназначены для внутренней сети; входной аутентификации Analyzer
пока нет. Перед внешним размещением потребуется аутентификация на границе сервиса.

## Проверки

```bash
# Сервис, транспорт, парсер, ошибки и совместимость с закреплённым SPAS:
uv run --frozen --group integration pytest -q -m 'not live'
# Отдельные регрессии false allow из-за нарушения протокола:
uv run --frozen pytest -q tests/test_false_allow.py
# Ограничение raw-ответов до распаковки/копирования и отсутствие вызова Executor:
uv run --frozen pytest -q tests/test_gateway_response_limits.py
# Отчёт по реальной модели (после настройки переменных):
uv run --frozen python -m safety_analyzer.evaluation --repeats 3
# Контекстные команды: quality-v1, 90 кейсов × 5 повторов:
uv run --frozen python -m safety_analyzer.command_evaluation --repeats 5 --seed 42 \
  --policy-file datasets/command-policy.json
# Проверка оценщика на заведомо ошибочных провайдерах, без inference:
uv run --frozen python scripts/evaluation_controls.py
# Опциональная live-проверка с тем же датасетом:
SAFETY_RUN_LIVE=1 uv run --frozen pytest -q -m live
```

Выбрана `inception/mercury-decide:free` через OpenRouter и `decisions` API:
[подключение и запуск](docs/openrouter.md). После настройки `OPENROUTER_API_KEY`:

```bash
uv run --frozen --group integration python scripts/evaluate_openrouter.py
```

Offline-тесты не доказывают устойчивость решений модели. Live-тест пропускается
без явного включения и запрещает маршрут `stub`.
Отчёт содержит отдельные false allow, false block, ошибки и изменчивость результатов;
`reports/` игнорируется Git. Перед тестами настоящего Executor выполните
`uv run --frozen --group integration python scripts/prepare_executor_reference.py`
(также включено в `scripts/cloud-install.sh` и CI).
Подробности и ограничения: [оценка](docs/evaluation.md),
[датасет](datasets/README.md).

## Docker

```bash
docker build -t safety-analyzer:dev .
docker compose --profile stub up --build
```

Compose запускает только локальную заглушку и Analyzer, порт 8000 привязан к loopback.
Для настоящего Gateway используйте `docker run --env-file <secure-env-file> ...` с
переменными из таблицы. Loopback внутри контейнера относится к самому контейнеру;
укажите DNS-имя Gateway в общей Docker-сети. Образ запускается от непривилегированного
пользователя, зависимости устанавливаются из `uv.lock`, dev/integration пакеты не входят.
Docker healthcheck проверяет liveness; он не доказывает доступность модели.

В этой облачной среде Docker не разрешает DNS-имя proxy. Вспомогательный скрипт
передаёт его разрешённый адрес, proxy-настройки и CA bundle через временный BuildKit
secret; проверка TLS и hashes сохраняется:

```bash
python scripts/build_cloud.py
docker compose --profile stub up --no-build
```

## Где адаптировать при смене модели

1. Для новой модели уточнить native input template, точные метки и категории в `docs/models.md`.
2. Изменить `POLICY`, `payload()` и `parse_model_output()` в `gateway.py` под её контракт;
   не распознавать `safe` как подстроку и не делать автоматический fallback формата.
3. Создать один явный маршрут Gateway на выбранный backend. Для Chat проверить
   temperature, лимит токенов и json_output; для Choice использовать decisions API.
4. Согласовать метки датасета и пороги, выполнить live-прогоны; затем закрепить версию
   модели и новую версию политики. Выбрана Mercury; реальная оценка и утверждение политики
   пока не завершены.
