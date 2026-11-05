# Safety Checker

`RuleBasedSafetyChecker` проверяет команду и не выполняет её. Публичный метод один:

```python
async def check(command: list[str], context: ExecutionContextPort) -> dict[str, object]
```

`command` — готовый argv, например `["docker", "ps", "-a"]`. Checker не использует
`shlex`, не собирает строку и не меняет аргументы. Строки, включая `"pwd"`, получают
`DENY`. Аннотация реализации допускает `str` только для совместимости типов со
старым портом Executor; поддержки строкового выполнения нет.

Контекст содержит `incident_id` и `reason`: непустые строки до 2000 символов.
Он не задаёт права доступа и не подтверждает опасные действия.

Ответ содержит ровно два поля:

```json
{"decision": "ALLOW", "reason": "List containers"}
```

Другие решения — `DENY` и `REQUIRE_APPROVAL`. Запуск разрешён только при `ALLOW`.
Риск, идентификатор правила и копия argv остаются во внутреннем `SafetyResult`:
валидатор Executor не принимает дополнительные поля.

## Правила

Разрешённые исполняемые файлы: `pwd`, `uname`, `ls`, `cat`, `docker`. Неизвестные
программы, пути к исполняемым файлам, оболочки и интерпретаторы запрещены.
Дополнительные запреты из `SafetyPolicy.denied_executables` имеют приоритет.

| Команда | Решение и ограничения |
| --- | --- |
| `pwd` | `ALLOW`, без аргументов |
| `uname` | `ALLOW`, только `-a`, `-s`, `-r`, `-m`, `-n`, `-v` |
| `ls` | `ALLOW`, только разрешённые точные пути; флаги `-l`, `-a`, `-h`, `-la`, `-al`, `-lh`, `-lah`, `-alh` перед путями |
| `cat` | `ALLOW`, только разрешённые точные пути, без флагов |
| `docker ps` | `ALLOW`, без флагов либо с одним `-a` / `--all` |
| `docker logs --tail N -- NAME` | `ALLOW`, `N` от 1 до 1000, контейнер из конфигурации |
| `docker restart -- NAME` | `REQUIRE_APPROVAL`, контейнер из конфигурации |
| Остальные формы | `DENY` |

Все глобальные флаги Docker запрещены: `--debug`, `-D`, `--host`, `--context`,
`--config` и другие. Например, `["docker", "--debug", "rm", "smth"]` получает
`DENY`. Флаги после подкоманды также допускаются только по таблице.
Удаление, `exec`, `run`, плагины и неизвестные подкоманды Docker запрещены.

Пустой список/аргумент, неправильные типы, управляющие и не-ASCII символы,
shell-операторы, подстановки и шаблоны (`;`, `&&`, `|`, `>`, `$`, `*` и т. п.)
запрещены. Хотя без shell операторы не исполняются, checker пока их не допускает.
Общая длина аргументов с учётом одного разделителя между ними — не более 4096
символов. Пробелы и кавычки внутри аргумента остаются буквальными символами.

Риски: разрешённое чтение — `LOW`, перезапуск — `MEDIUM`, неподдерживаемый ввод —
`HIGH`, явно запрещённые программы и Docker-действия — `CRITICAL`.

## Подключение

Код checker и Executor должен находиться в одном checkout, например в общей
ветке интеграции. Два checkout через `PYTHONPATH` не объединяют пакет `executer`:
Python выбирает обычный пакет Executor с `__init__.py`. Менять его не требуется.

```python
from executer.safety_checker import RuleBasedSafetyChecker, SafetyPolicy
from executer.server_mcp.Server import create_server

checker = RuleBasedSafetyChecker(SafetyPolicy(
    readable_paths={"/srv/spas/app.log"},
    containers={"backend"},
))
server = create_server(checker=checker)
```

Без конфигурации доступ к файлам и контейнерам закрыт. Политику задаёт приложение,
не Worker и не входной запрос. Отдельного `ExecutorSafetyChecker` больше нет.

Семёну нужно обновить входную модель команды и `SafetyCheckerPort` на `list[str]`,
а Runner — на `asyncio.create_subprocess_exec(*argv)` без `/bin/bash -c`.
Формат ответа остаётся прежним: `decision` и `reason`, тип `dict[str, object]`.
`ExecutorSafetyDecision` описывает поля внутри checker и не является возвращаемым
типом метода: `TypedDict` не совместим с `dict[str, object]` в текущем порте.

Executor должен сохранить аргументы перед проверкой и выполнить те же значения,
не меняя их и не разбирая повторно. Например, после валидации входного `list[str]`:

```python
import asyncio
from executer.server_mcp.Safety_contract import SafetyDecision

argv = tuple(command)
decision = SafetyDecision.model_validate(await checker.check(list(argv), context))
if decision.decision == "ALLOW":
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
```

Сбой проверки или невалидный ответ должны запрещать запуск. `REQUIRE_APPROVAL`
не разрешает выполнение автоматически. Executor также отвечает за доверенный
`PATH`, окружение, права, симлинки, доступ к Docker и ограничения процесса.

## Проверки

```sh
python3 -m unittest discover -s tests -v
```

В отдельной ветке checker проходят 23 теста; 5 проверок Executor пропускаются.
Для проверки с его исходниками нужен Python 3.12+ и зависимости:

```sh
python3 -m pip install -r /path/to/executor/Requirements-executor.txt 'mypy>=1.10,<2'
python3 scripts/check_executor_integration.py /path/to/executor
```

Скрипт собирает компоненты в одном временном каталоге с настоящим
`executer/__init__.py`, запускает тесты и mypy, затем удаляет каталог. Исходные
checkout не меняются. С Executor `workeragent/executermcp` на `79f4fd0` проходят
28 тестов и проверка типов текущего порта и предложенного argv-порта.

В этом Executor пока строковый ввод: тесты проверяют валидатор ответа и запрет
запуска строковых команд. Полное выполнение argv через MCP нужно проверить
после изменений Семёна; эти тесты не заявляют, что Runner уже переведён на argv.
