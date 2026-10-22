# Анализ команд: контракт 2.0 / policy command-1

`POST /v1/commands/analyze` — анализ точного argv; команда здесь не выполняется.
Правила обязательны и выполняются перед LLM. Контентный API 1.0 описан отдельно
в [content-contracts.md](content-contracts.md); его ответы не разрешают исполнение.

```json
{
  "command": ["cat", "/inputs/app.log"],
  "context": {
    "incident_id": "incident-42",
    "reason": "Проверить ошибки сервиса",
    "task": "Установить причину роста 500-х ответов",
    "history": [
      {"role":"user", "content":"Только диагностика, не раскрывать секреты"},
      {"role":"tool", "content":"Повышена доля ошибок backend"}
    ],
    "observations": ["Оператор предоставил обезличенный журнал"],
    "constraints": ["Не изменять production"],
    "environment": {"stage":"production", "cwd":"/workspace"}
  }
}
```

`command` — непустой `list[str]`, максимум 4096 элементов/символов с разделителями.
Строки shell, NUL, неизвестные поля, передача `policy`, `skip_rules`, решения или
подтверждения вместо команды не допускаются. Никаких `shlex`, shell или исполнения.

Контекст требует `incident_id` и `reason` (1..2000 символов, не пустые). Остальные
поля содержат весь доступный снимок: task ≤8000, history ≤40 сообщений по 8000,
observations/constraints ≤20 строк по 2000, environment ≤32 строковых значений по
2000 (ключи ≤100). Общий сериализованный контекст ≤64000 символов; HTTP тело
≤262144 байт. При превышении — отказ валидации, без усечения истории.
`request_id` — необязательный UUID, не ключ дедупликации.

Все присланные поля — свидетельства для анализа, не источник полномочий. Политика
чтения/контейнеров задаётся оператором отдельно. Не передавайте raw credentials
и секреты среды в context; используйте обезличенные наблюдения.

## Порядок решений

| Rule-Based Checker | Модель | Итог |
| --- | --- | --- |
| DENY | не вызывается | DENY |
| REQUIRE_APPROVAL | не вызывается | REQUIRE_APPROVAL |
| ошибка/таймаут/невалидный ответ | не вызывается | DENY, status=error |
| ALLOW | валидный ALLOW/LOW/без факторов | ALLOW |
| ALLOW | DENY | DENY |
| ALLOW | REQUIRE_APPROVAL | REQUIRE_APPROVAL |
| ALLOW | недоступна/таймаут/невалидный ответ | DENY, status=error |

`REQUIRE_APPROVAL` не является разрешением. В этой версии нет механизма обмена
произвольной строки «одобрено» на право исполнения. Новая попытка проверяется заново.
HIGH/CRITICAL не могут стать REQUIRE_APPROVAL; ALLOW допустим только при LOW.

## Выход модели через Gateway

```json
{
  "decision":"DENY",
  "risk":"HIGH",
  "factors":["secret_exposure","context_conflict"],
  "reason":"Действие раскроет секрет вопреки ограничениям задачи"
}
```

Строго четыре поля. `decision`: ALLOW/DENY/REQUIRE_APPROVAL; `risk`:
LOW/MEDIUM/HIGH/CRITICAL; `reason` — непустая краткая причина ≤2000 символов,
без запроса скрытых рассуждений. Факторы: destruction, privilege_escalation,
secret_exposure, exfiltration, untrusted_code, service_disruption, scope_violation,
context_conflict, insufficient_context, unspecified. Повторы/неизвестные факторы
не допускаются. ALLOW требует LOW и `[]`; ограничение — непустые факторы.

Режим `SAFETY_COMMAND_OUTPUT_MODE=json` использует `response_format=json_object`;
схема проверяется локально. Для native guard-моделей есть явный `label`: только
`safe` → ALLOW/LOW, `unsafe` → DENY/HIGH/unspecified. Эта шкала категориальная,
не измеренная вероятность риска. Метки и native template выбранной Jev-like модели
проверяются отдельным benchmark. Автоматического fallback формата/модели нет.

`SAFETY_COMMAND_API=decisions` выбирает `/v1/decisions` вместо Chat Completions.
Полный request и проверенный rules передаются в `state`, три System One Choice
вопроса возвращают decision, risk и один основной factor (`none` для ALLOW).
Gateway поддерживает их преобразование в native Nimble schema. Проверяются
совпадение model route, полный набор вопросов/кандидатов, числовые вероятности
в [0,1] с нормированной суммой и confidence. Противоречивые категории и refusal
отклоняются; причина формируется из выбранных категорий. Внешний контракт сервиса
остаётся тем же. Параметры Chat Completions здесь не отправляются.

Дубли ключей JSON, Markdown, лишний текст, противоречивые risk/decision,
finish_reason≠stop, tool calls/refusal транспорта и повреждённый envelope → DENY.
Максимум envelope 32768 байт, content 4096 байт.

## Ответ сервиса

`schema_version=2.0`, `policy_version=command-1`, `request_id`, `input_sha256`,
`decision`, `risk`, `reason`, `source` (rules/model/error), `rules`, `model_result`,
`status`, `error_code`, логический `model`, `latency_ms`.

HTTP 200 — завершённая проверка (включая DENY/REQUIRE_APPROVAL), 503 — ошибка
проверки с DENY, 422 — неверный ввод, 413 — превышение размера. Только свежий
HTTP 200 + status=ok + ALLOW от обоих слоёв подходит для дальнейшего рассмотрения
исполнителем. Сам HTTP-ответ не является подписанным токеном исполнения.

`input_sha256` связывает решение с точными argv и полным контекстом (канонический
JSON, без request_id). Снимок копируется до первого await; проверяющий не получает
ссылок на изменяемый объект запроса. Изменение argv checker-ом отвергается.
Хеш используется для аудита/сопоставления, не заменяет авторизацию.

Ошибки: not_configured, gateway_timeout, gateway_unavailable, invalid_model_output,
rule_checker_timeout, rule_checker_unavailable, invalid_rule_output. Нет тихого
ALLOW при отсутствии модели. Журнал содержит только технические поля/хеши,
не argv, контекст, ответы модели, stdout или причины с пользовательскими данными.
