# Проверка Mercury через OpenRouter: 28 октября 2026

Модель: **inception/mercury-decide:free**, провайдер **OpenRouter.ai**.
Статус реального прогона: **not_run**. Запросов к реальной модели: **0**.
Метрики качества, false allow и стабильности реальной Mercury отсутствуют.
[Машинный отчёт](openrouter-mercury-2026-10-28.json).

## Что подготовлено и проверено

Конфигурация `config/openrouter-mercury.toml` фиксирует один backend, модель,
endpoint `/api/alpha/decisions`, System One Choice и отключённый fallback.
`scripts/evaluate_openrouter.py` поднимает настоящий локальный SPAS HTTP Gateway,
создаёт временные credentials/client key и проверяет три контекстных решения.
После успешного получения модельных ответов запускается quality-v1: 90 кейсов,
пять повторов, 350 ожидаемых модельных проверок плюс три probe.

Между модельными запросами выбрана пауза не менее 3.1 секунды; она не расходует
deadline Analyzer. Это частота запуска, а не подтверждённая квота OpenRouter.
Временные credentials и сервер очищаются после завершения. Модель, destination
и отсутствие fallback проверяются до передачи credentials.

Проверки реализации: **435 passed, 2 skipped** (live-тесты), Ruff check/format прошли.
Девять OpenRouter-тестов проверяют передачу полного context через настоящий
SPAS Gateway, все три решения, реальный локальный TCP workflow, остановку при
ошибке авторизации, очистку credentials и отклонение подменённой конфигурации.
В этих тестах удалённый inference подменён. Они не являются результатами Mercury.
Отдельно проверено, что паузы не создают искусственных timeout внутри Analyzer.

## Внешние блокеры

1. В процессе отсутствует `OPENROUTER_API_KEY`; в стандартном хранилище SPAS
   не обнаружено аккаунтов/ключей. Автоматический запуск завершился кодом 2,
   записав `preflight.json` со статусом not_run, без запуска benchmark.
2. Публичный GET каталога OpenRouter завершился `curl exit 56`:
   `CONNECT tunnel failed, response 403`. То же произошло после запроса
   расширенного выполнения. Ответ пришёл от сетевого прокси до соединения
   с OpenRouter; HTTP-статус самого провайдера неизвестен. Доступность указанной
   модели в текущем каталоге проверить не удалось.

В черновике среды сохранены требование секрета `OPENROUTER_API_KEY`, разрешённый
домен `openrouter.ai` и инструкции запуска. Это сохранение конфигурации,
а не применение ключа/egress к текущему процессу. Для продолжения нужно заполнить
ключ, сохранить настройки и опубликовать среду. Суффикс `:free` не заменяет API-ключ.
GPU и отдельный внешний Gateway не требуются.

После применения настроек:

```bash
cd /workspace/spas/executer/safety_checker
uv run --frozen --group integration python scripts/evaluate_openrouter.py
```

Workflow сохранит `preflight.json`, затем при запуске полного benchmark —
`quality.json` и `quality.md` в `reports/openrouter-mercury/`.
Полная [инструкция](../openrouter.md). Команды из датасета не исполняются.
