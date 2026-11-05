# СПАС: интеграционный MVP

Рабочая ветка: `integration/mvp`. Полный цикл проходит через Orchestrator,
Diagnostic, Worker/Planner и Executor MCP до повторной Verification.
Инфраструктурное действие **явно симулируется** внутри Executor: это безопасная
демонстрация взаимодействия сервисов, а не управление Docker или хостом.

## Первый запуск

Нужны Docker Engine с Compose v2 и Python 3 для команды демонстрации.
Из одного checkout:

```bash
cp config/mvp.env.example .env.mvp
# Замените четыре demo-токена в .env.mvp на разные случайные значения.
docker compose --env-file .env.mvp -f compose.mvp.yaml up --build -d --wait
python3 -m apps.mvp.demo
```

Ожидается JSON: `status: resolved`, четыре этапа в `history`, отдельные
`remediation_plan` и `execution_results`, `verification.resolved: true`.
Первый запуск исправляет симулированный сбой. Последующие инциденты увидят уже
здоровый сервис и выполнят Verification без изменения состояния.

API доступен только на `http://127.0.0.1:8010`: `POST /incidents` с `{}` создаёт
инцидент, `POST /incidents/{id}/process` обрабатывает его, `GET /incidents/{id}`
показывает результат. Повторная обработка завершённого ID ничего не исполняет.

```bash
# Диагностика запуска:
docker compose --env-file .env.mvp -f compose.mvp.yaml ps
docker compose --env-file .env.mvp -f compose.mvp.yaml logs --tail=100
# Остановка (Gateway credentials сохраняются в volume):
docker compose --env-file .env.mvp -f compose.mvp.yaml down
# Следующий up создаст новый симулированный сбой и новые журналы попыток.
```

По умолчанию используется явно обозначенный `StubPlanner`, без секретов и LLM.
Gateway запускается в том же Compose. Для LLM-планирования настройте провайдера
через `docker compose --env-file .env.mvp -f compose.mvp.yaml exec gateway spas-llm`
(команды приведены ниже), затем задайте `PLANNER_MODE=gateway`, `GATEWAY_MODEL`
и `GATEWAY_API_KEY` в `.env.mvp` и пересоздайте Worker. Ошибки Gateway не приводят
к исполнению. Текстовый план LLM остаётся предложением; argv задаёт доверенный runbook.

## Проверка

```bash
uv sync --locked --extra dev --extra colab --extra claude
uv run --no-sync pytest -q
# Дополнительно: реальный subprocess Runner в защищённом контейнере
docker build -f executer/server_mcp/Dockerfile -t spas-executor:local .
uv run --no-sync python -m executer.server_mcp.Smoke_test
```

Включены прежние регрессии компонентов, отказы безопасности и этапов, отрицательная
Verification, таймаут/отмена после исполнения без ответа, защита от повторов,
а также реальный сетевой тест четырёх процессов (HTTP + MCP). CI отдельно запускает
Compose и демонстрацию. Локальные сетевые тесты используют только loopback.

[Архитектура, контракты, решения по веткам и ограничения](docs/mvp-architecture.md).
MVP хранит состояние в памяти и рассчитан на один процесс каждого сервиса.
Рестарт не восстанавливает незавершённые операции. Approval ещё не реализован:
`REQUIRE_APPROVAL` блокирует действие. Docker socket не передаётся ни одному сервису.

---

# СПАС — система предотвращения и автоустранения сбоев

Мультиагентная система для автоматического обнаружения,
диагностики и устранения сбоев и инцидентов.

## Запуск

На Linux с Docker Engine и Docker Compose выполните из корня проекта:

```bash
docker compose up --build -d
```

Сборка запускает шлюз моделей на `http://127.0.0.1:8008`.
Python и Claude Code входят в образ; настройки и аккаунты сохраняются в томе Docker.
Откройте терминальный интерфейс СПАС:

```bash
docker compose run --rm tui
```

На главном экране доступны три способа подключения:

- **Подписки ChatGPT и Claude** — вход в аккаунт и выбор модели по подписке.
- **API моделей** — облачные и локальные OpenAI-совместимые сервисы. Укажите адрес,
  модель и API-ключ либо выберите доступ без ключа. Поддерживаются Chat Completions,
  Responses и API моделей решений: Decisions и System One.
- **Google Colab** — запуск модели на CPU или GPU, управление средой и терминал.
  Вход через Colab CLI не требует собственного проекта Google или OAuth-клиента.

В разделе **Тестирование** выберите подключение. Для LLM введите текст — ответ
начнёт поступать по мере генерации, если сервис поддерживает потоковый режим.
Для модели решений Jev-like выберите режим, введите состояние, вопрос и критерии.
Запрос отправится сразу после ввода. Пинг доступен в меню подключений.

Маршруты, ключи доступа и журнал запросов находятся в разделе **Настройки шлюза**.
Подробная настройка и описание API — в [руководстве по шлюзу моделей](llm/LLM.md).

## Проверки для разработки

На Linux с Python 3.12+ и uv выполните из корня проекта:

```bash
uv run --locked --extra dev --extra colab python -m pytest -q
```

Команда установит проект, зависимости тестов и Colab CLI в `.venv` и запустит
тесты в этом окружении. Отдельная команда `pytest` может использовать системный
Python, в котором отсутствуют `fastapi` и `pytest-asyncio`.

## Основная идея

- Orchestrator — основная модель, управляющая всем конвейером решения проблемы.
- Diagnostic Agent — агент, который анализирует и исследует состояние и окружение системы. Он собирает необходимые данные и определяет причину проблемы.
- Worker Agent — агент, отвечающий за устранение ошибки. Он получает предполагаемую причину, формирует план и выполняет его через Executor MCP.
- Executor MCP — интерфейс для выполнения действий над инфраструктурой.
- Memory — хранилище информации о прошлых инцидентах, документации и опыте работы системы.
- LLM Gateway — интерфейс доступа к LLM.

## Обработка инцидента

```text
Incident
   ↓
Orchestrator
   ↓
Diagnostic Agent
   ↓
Diagnosis
   ↓
Worker Agent
   ↓
Plan
   ↓
Executor MCP
   ↓
Safety Checker
   ↓
Tools
   ↓
Execution Result
   ↓
Worker Agent
   ↓
Verification
   ↓
Resolved / Replan / Re-diagnosis
```

## Основные принципы

- Агенты напрямую не взаимодействуют с инфраструктурой.
- Доступ к моделям настраивается через шлюз, который объединяет подключения к разным сервисам.
- Мультиагентная система позволяет разделить процесс обработки инцидента, что делает поведение системы более стабильным и контролируемым.
