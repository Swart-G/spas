# Safety Analyzer

База сервиса безопасности для СПАС. Пока модель не выбрана, используется явная
локальная заглушка. Политика, категории и метки — **предложение `draft-1` для
согласования**, а не утверждённый контракт Jev-like модели.

Реализованы FastAPI API, строгая проверка ответа модели, fail-closed обработка ошибок,
адаптер LLM Gateway, JSON-логирование без содержимого запросов, датасет, offline и
опциональные live-тесты, оценка стабильности/false allow и контейнеризация.

Совместимость проверяется с [SPAS Gateway](https://github.com/Swart-G/spas/tree/2968247a5bed416054a66bd08ef18589f77230ed)
через его настоящий HTTP API и `RemoteInferenceAdapter` с подменённым inference.
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
# Отчёт по реальной модели (после настройки переменных):
uv run --frozen python -m safety_analyzer.evaluation --repeats 3
# Опциональная live-проверка с тем же датасетом:
SAFETY_RUN_LIVE=1 uv run --frozen pytest -q -m live
```

Offline-тесты не доказывают устойчивость решений модели. Live-тест пропускается
без явного включения и запрещает маршрут `stub`.
Отчёт содержит отдельные false allow, false block, ошибки и изменчивость результатов;
`reports/` игнорируется Git. Подробности и ограничения: [оценка](docs/evaluation.md),
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

## Где адаптировать после выбора модели

1. Уточнить модель, native input template, точные метки и категории в `docs/contracts.md`.
2. Изменить `POLICY`, `payload()` и `parse_model_output()` в `gateway.py` под её контракт;
   не распознавать `safe` как подстроку и не делать автоматический fallback формата.
3. Создать один явный маршрут Gateway на выбранный backend; проверить возможности
   `temperature`, лимита токенов и `json_output`. Текущий адаптер требует первых двух.
4. Согласовать метки датасета и пороги, выполнить live-прогоны; затем закрепить версию
   модели и новую версию политики. Выбор модели и утверждение политики пока не выполнены.
