# Mercury через OpenRouter и SPAS Gateway

Выбранная модель: **inception/mercury-decide:free**. Протокол — System One Choice,
endpoint OpenRouter — `https://openrouter.ai/api/alpha/decisions`, endpoint Analyzer →
SPAS Gateway — `/v1/decisions`. Используется закреплённая зависимость SPAS Gateway
из группы `integration`, без fallback на другую или платную модель.
Конфигурация: [openrouter-mercury.toml](../config/openrouter-mercury.toml).

## Подключение

Нужен только `OPENROUTER_API_KEY` в секретных настройках среды и HTTPS-доступ
к `openrouter.ai`. Суффикс `:free` не отменяет авторизацию и квоты провайдера.
В облачной среде ключ должен доставляться на этот домен через поддерживаемый
прокси. Требование ключа и домена сохранено в черновике настроек; значения секретов
в репозиторий и чат не передаются. После заполнения сохраните настройки и опубликуйте
среду, затем повторите запуск. CUDA, локальные веса и Colab не требуются.

Скрипт сам поднимает настоящий SPAS Gateway на свободном loopback-порту,
создаёт временный Gateway client key, подключает аккаунт `safety-openrouter`
и маршрут `safety-mercury`. Дополнительные SAFETY_GATEWAY_BASE_URL,
SAFETY_GATEWAY_API_KEY и SAFETY_MODEL для этого скрипта не нужны.
Секрет читается из окружения, хранится стандартным зашифрованным CredentialStore
временного Gateway и очищается при завершении; ключи не выводятся в лог.
Нет подмены удалённого транспорта в рабочем запуске.

```bash
cd /workspace/spas/executer/safety_checker
uv sync --frozen --group integration
# Сначала проверить доступ и три контекстных решения:
uv run --frozen --group integration python scripts/evaluate_openrouter.py --probe-only
# Затем полный benchmark (или сразу выполнить эту команду):
uv run --frozen --group integration python scripts/evaluate_openrouter.py
```

## Что проверяется

Probe берёт три варианта семейства `tool-injection` из quality-v1: одинаковый argv,
но безопасный, опасный и неоднозначный контекст. Полный request и результат правил
проходят через реальный HTTP Gateway. Ожидаемые решения остаются в оценщике.
Probe сохраняет hash входа, ожидаемое и фактическое решение, статус и latency;
текст контекста и причины в отчёт не попадают. Ошибка доступа/транспорта останавливает
запуск до полного benchmark. Неверная классификация даёт ненулевой код probe,
а при полном запуске оценивается всем датасетом.

Полный прогон: 90 кейсов ×5 =450 анализов, 350 ожидаемых модельных проверок плюс три probe.
Seed=42. Между ожидаемыми модельными запросами по умолчанию не менее 3.1 секунды;
пауза выполняется **вне** 4-секундного deadline Analyzer. Правила по-прежнему
проверяются на каждом анализе и не могут быть отменены метками или моделью.
`--model-interval-seconds` меняет паузу, `--repeats` — число повторов (минимум два).
Это выбранная частота запуска, а не подтверждение доступной аккаунту квоты.
Полный прогон занимает не менее примерно 18 минут при такой частоте; провайдер
может ограничить суточную квоту или доступность. Автоматических повторов нет.

Артефакты в `reports/openrouter-mercury/` (или `--output-dir`):

- `preflight.json`: выбранный provider/model/endpoint, config hash, ревизия Gateway,
  результат probe либо `not_run` и недостающие prerequisites.
- `quality.json` и `quality.md`: полный benchmark, false allow, ошибки,
  model coverage, стабильность и срезы development/holdout. Если benchmark
  не запускался, новые quality-отчёты не создаются; смотрите время текущего preflight.

Модельный alias фиксирован, ревизия управляемых провайдером весов неизвестна и
не объявляется закреплённой. Runner проверяет модель, destination и отсутствие
fallback до передачи credentials. Команды из датасета не исполняются, MCP Executor
в этом workflow не вызывается. Ошибки модели остаются DENY/status=error.

Коды завершения: 0 — probe/quality gate пройден; 1 — решения не проходят пороги;
2 — отсутствует ключ, ошибка запуска или probe не получил валидный модельный ответ.
Контрольные и mock-тесты не подтверждают реальное качество Mercury.

Текущий статус и фактические ограничения:
[отчёт 28 октября 2026](reports/openrouter-mercury-2026-10-28.md).
