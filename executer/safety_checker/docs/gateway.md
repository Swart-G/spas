# Интеграция со SPAS LLM Gateway

Проверенный reference commit: `d311eedf6e75d1adcbe6668101e6bc69a1ed4734`.
Endpoints: `POST /v1/chat/completions` и `POST /v1/decisions`;
обязательный `Authorization: Bearer <client key>`.
Для Chat Completions:
Запрос: логический `model`, system/user messages, `temperature=0.0`, `max_tokens=128`,
`stream=false`, `n=1`. Только классификатор задаёт параметры; HTTP-клиент Analyzer
не может менять модель, системный prompt или формат.

Для команд используется отдельный `command_payload`: точный argv, полный context
и проверенный результат правил сериализуются одним user JSON; system policy
задаётся только сервисом. JSON-ответ содержит decision/risk/factors/reason, лимит
по умолчанию 512 токенов, общий deadline анализа 4s. Командные настройки отделены
от прежнего контентного режима; LLM не вызывается после rule DENY/REQUIRE_APPROVAL.

В SPAS `ResponseFormat.type` уже принимает `text` и `json_object`. В режиме label
Analyzer отправляет `text`, в JSON — `json_object`; валидирует схему локально.
`json_schema` не поддерживается текущим SPAS и не отправляется.
`remote_inference` поддерживает temperature и output limit; JSON требует
`remote_json_output=true` и фактической поддержки JSON удалённым inference.
Colab managed сервер текущего SPAS принимает текстовый режим; это подходит заглушке
native label модели. Иные backends могут не поддерживать temperature/лимиты — текущий
адаптер не переключается на них автоматически.

Для реализованной в SPAS `bespokelabs/Bespoke-Nimble-9B` задайте
`SAFETY_COMMAND_API=decisions`. Analyzer отправляет полный request и результат правил
в System One `state`, а decision/risk/основной factor — в трёх Choice вопросах.
SPAS преобразует этот контракт в native Nimble schema. Параметры Chat Completions
не отправляются. Нужен запущенный GPU runtime, подключённый к Gateway;
наличие scorer в репозитории не означает, что inference уже доступен.
Ревизия scorer и требования: [models.md](models.md).

Пример будущего маршрута (placeholder, не запущенная модель):

```toml
[backends.safety]
provider = "remote_inference"
account = "safety-runtime"
model = "REPLACE_WITH_SELECTED_MODEL"
base_url = "https://inference.example.invalid/v1"
remote_json_output = false
timeout_seconds = 30
max_concurrency = 1

[routes]
safety-default = ["safety"]
```

Provider key хранится в vault SPAS, Analyzer получает только Gateway client key.
Не используйте реальный endpoint до настройки TLS, сетевого доступа и credentials.
Нет повторов, переходов по redirect, автоматического переключения формата или модели;
маршрут Gateway для оценки должен содержать один backend без резервной модели.
Для HTTP разрешён внутренний Gateway; через внешнюю сеть используйте HTTPS.
HTTP-клиенты Analyzer и оценки используют `trust_env=false`: `HTTP_PROXY`,
`HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` и их варианты в нижнем регистре игнорируются.
Соединение идёт напрямую с настроенным Gateway; `socksio` не требуется. Стандартная
проверка TLS остаётся включённой; переменные `SSL_CERT_FILE`/`SSL_CERT_DIR` также
не наследуются. Переданный извне тестовый HTTP-клиент управляется вызывающим кодом.

## Ограничение HTTP-ответа

Оба протокола отправляют `Accept-Encoding: identity`, переопределяя настройки
переданного клиента. Ответ должен не иметь `Content-Encoding` либо содержать
единственное `identity` (регистр не важен). gzip, deflate, br, zstd, неизвестные,
пустые и составные значения отклоняются **до первого чтения тела**.
Автоматическая распаковка HTTPX не вызывается: используется `aiter_raw()`.

Максимальный размер envelope — 32768 байт. Если указан Content-Length, проверяются
его числовой формат и лимит до чтения тела. При потоковом чтении размер каждого
чанка проверяется относительно оставшегося бюджета **до** добавления в буфер,
включая ответы без Content-Length и с заниженным значением. Ответ ровно в лимит
допускается; размер считается в байтах. После отказа не запрашиваются новые чанки,
response закрывается. Тела ответов со статусом, отличным от 200, не читаются.

Неподдерживаемое кодирование, некорректный Content-Length или превышение лимита
дают `invalid_model_output`. Командный анализ возвращает DENY/status=error,
model_result=null. CheckedExecutor не вызывает MCP Executor. Доступность Gateway
или ошибка чтения не становятся разрешением; автоматического повтора нет.
Регрессии: `tests/test_gateway_response_limits.py`.

## Проверка реального inference

В настройках среды укажите URL Gateway (`SAFETY_GATEWAY_BASE_URL`, включая `/v1`),
его логический маршрут (`SAFETY_MODEL`) и клиентский ключ (`SAFETY_GATEWAY_API_KEY`,
секретным значением, не в репозитории). Для Nimble включайте native протокол явно:

```bash
SAFETY_COMMAND_API=decisions uv run --frozen --group integration \
  python -m safety_analyzer.command_evaluation \
  --dataset datasets/quality-v1.jsonl --policy-file datasets/command-policy.json \
  --repeats 5 --seed 42 --output reports/live-nimble-quality.json
```

Полный контекст отправляется без обрезки. Команды не исполняются; опасные
действия в датасете — входные данные для анализа. Перед сравнением закрепите
backend и ревизии весов/scorer (`--backend-revision` записывает сведения оператора).
Коды CLI: 0 — gate пройден, 1 — качество не прошло пороги, 2 — ошибка конфигурации.
JSON и Markdown содержат false allow и стабильность отдельно от ошибок inference.
Последняя проверка и доступность runtime: [отчёт](reports/http-response-limits.md).

`tests/test_spas_contract.py` проверяет весь путь Analyzer → настоящий SPAS HTTP API →
Router → RemoteInferenceAdapter → подменённый remote inference. Зависимость закреплена
по commit в группе `integration`; она не входит в production-образ. Это доказывает
совместимость транспорта/контракта, но не доступность реального inference.

После выбора модели измените адаптер под native template (системный prompt не всегда
подходит guard-моделям), точные метки и категории, затем запустите оценку повторно.
Если потребуется новый формат (например, logits или специальный response schema),
изменение SPAS должно идти вместе с обновлёнными контрактными тестами.
