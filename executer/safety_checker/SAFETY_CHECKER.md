# Safety Checker

`RuleBasedSafetyChecker.check(command, context)` — асинхронная проверка готового
`list[str]`, например `["docker", "ps", "-a"]`. Строки команд запрещены. Checker
не запускает программы, не использует `shlex` и не меняет аргументы.

Контекст содержит только `incident_id` и `reason`: непустые строки до 2000
символов. Ответ — `dict[str, object]` с двумя полями:

```json
{"decision": "ALLOW", "reason": "List containers"}
```

Другие решения — `DENY` и `REQUIRE_APPROVAL`. Только `ALLOW` допускает запуск.
`SafetyResult` хранит риск, правило и копию argv внутри checker. Эти поля не
передаются Executor: его валидатор запрещает дополнительные поля.

## Правила

| Команда | Ограничения |
| --- | --- |
| `pwd` | Без аргументов |
| `uname` | Только `-a`, `-s`, `-r`, `-m`, `-n`, `-v` |
| `ls` | Точные пути из политики; перед ними допустимы `-l`, `-a`, `-h`, `-la`, `-al`, `-lh`, `-lah`, `-alh` |
| `cat` | Точные пути из политики, без флагов |
| `docker ps` | Без флагов либо один `-a` / `--all` |
| `docker logs --tail N -- NAME` | `N` от 1 до 1000, разрешённый контейнер |
| `docker restart -- NAME` | Разрешённый контейнер, всегда `REQUIRE_APPROVAL` |

Остальные формы получают `DENY`. Глобальные флаги Docker (`--debug`, `-D`,
`--host`, `--context`, `--config` и другие), удаление, `exec`, `run` и плагины
запрещены. Дополнительная denylist имеет приоритет над разрешающими правилами.

Пустые аргументы, неверные типы, shell-операторы, подстановки, шаблоны,
управляющие и не-ASCII символы запрещены. Общий лимит — 4096 символов с учётом
разделителей между аргументами. Пробелы и кавычки внутри аргумента буквальны.
Риски: чтение — `LOW`, перезапуск — `MEDIUM`, неподдерживаемый ввод — `HIGH`,
явные запреты — `CRITICAL`.

## Права агентов

Роль задаёт приложение при создании Executor, а не агент в запросе.
По умолчанию используется `diagnostic`.

| Роль | Доступ |
| --- | --- |
| `diagnostic` | `pwd`, `uname`, `ls`, `cat`, `docker ps/logs`; аргументы и ресурсы всё равно проверяет checker |
| `worker` | Может запрашивать изменения, но запуск зависит от решения checker; подтверждение не выдаётся автоматически |

Executor отклоняет недоступный Diagnostic тип операции **до** обращения к
checker. Такая команда имеет `status=denied`, `safety_checked=false` и событие
`agent_access_denied`. Роль Worker не отменяет запреты или требования approval.

```python
from executer.safety_checker import AgentRole, RuleBasedSafetyChecker, SafetyPolicy
from executer.server_mcp.Server import create_server

checker = RuleBasedSafetyChecker(SafetyPolicy(readable_paths={"/inputs/app.log"}))
diagnostic = create_server(checker=checker, agent_role=AgentRole.DIAGNOSTIC)
worker = create_server(checker=checker, agent_role=AgentRole.WORKER)
```

Это разные MCP-соединения. Оркестратор/оператор должен дать Diagnostic доступ
только к его серверу: код не аутентифицирует клиентов общего сетевого сервера.
Не передавайте выбор соединения или запускаемые CLI-аргументы модели.
Поля `role`, `agent_role`, `approved`, `policy` и другие дополнительные поля
в контексте или аргументах инструмента отклоняются. Текст `reason` прав не даёт.
При необходимости каждому серверу назначается свой файл политики чтения.

Для запуска контейнеров:

```sh
bash executer/server_mcp/Run_docker.sh --agent-role diagnostic
bash executer/server_mcp/Run_docker.sh --agent-role worker
```

В текущем Docker-профиле управление Docker хоста запрещено обеим ролям.
`--without-safety` доступен только с явно выбранной ролью Worker и предназначен
для изолированной разработки; подключать его к автономным агентам нельзя.

## Интеграция и проверки

Checker и Executor находятся в одном checkout. Подключение двух checkout через
`PYTHONPATH` не работает из-за обычного пакета `executer` с `__init__.py`.
Публичный контракт: `async check(list[str], context) -> dict[str, object]`.
Отдельного адаптера нет. Executor сохраняет argv до проверки и выполняет тот
же набор аргументов через `create_subprocess_exec`, без shell.

Сбой, таймаут или невалидный ответ запрещает запуск. После `ALLOW` Executor
дополнительно проверяет пути чтения и применяет ограничения Docker-профиля.
[Журнал событий и отказы](../audit/AUDIT.md).

```sh
python3 -m pip install -r requirements-safety-dev.txt
python3 scripts/check_executor_integration.py
```

Скрипт запускает правила, тесты ролей, интеграцию MCP/Runner/Audit Log и mypy
против настоящего порта Executor. Процессы и сигналы в этих тестах подменены;
реальные MCP-сеансы и изоляцию Linux проверяет отдельный Docker smoke-тест:

```sh
docker build -f executer/server_mcp/Dockerfile -t spas-executor:local .
python3 -m executer.server_mcp.Smoke_test
```

CI: `.github/workflows/executor-safety.yml`, Python 3.12/3.13, Ruff и отдельный
Docker smoke-job. Проверки не требуют LLM, API-ключей или Colab.
