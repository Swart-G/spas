# Интеграция со SPAS LLM Gateway

Проверенный reference commit: `2968247a5bed416054a66bd08ef18589f77230ed`.
Endpoint: `POST /v1/chat/completions`, обязательный `Authorization: Bearer <client key>`.
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

`tests/test_spas_contract.py` проверяет весь путь Analyzer → настоящий SPAS HTTP API →
Router → RemoteInferenceAdapter → подменённый remote inference. Зависимость закреплена
по commit в группе `integration`; она не входит в production-образ. Это доказывает
совместимость транспорта/контракта, но не доступность реального inference.

После выбора модели измените адаптер под native template (системный prompt не всегда
подходит guard-моделям), точные метки и категории, затем запустите оценку повторно.
Если потребуется новый формат (например, logits или специальный response schema),
изменение SPAS должно идти вместе с обновлёнными контрактными тестами.
