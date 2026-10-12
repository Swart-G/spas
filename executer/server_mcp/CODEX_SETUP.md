# Запуск и проверка Executor

## 1. Собрать образ

Нужны Linux с ядром 5.3+ (pidfd), Docker Engine и доступ пользователя к Docker. Docker Compose не
требуется. Команды ниже выполняются из корня `spas`:

```sh
cd /path/to/spas
docker build -t spas-executor:local -f executer/server_mcp/Dockerfile .
```

После изменения Python-кода образ нужно пересобрать. Исходники не подключаются
в контейнер с хоста. Файл зависимостей — `requirements.txt` в корне проекта.

## 2. Проверить руками

```sh
bash executer/server_mcp/Run_docker.sh --manual
```

В приглашении `executor>` вводить по одной команде:

| Ввод | Ожидается |
| --- | --- |
| `pwd` | `/workspace`, succeeded, exit_code=0 |
| `uname -s` | Linux |
| `ls -la /workspace` | Список рабочей папки |
| `cat /etc/passwd` | denied: путь не разрешён |
| `pwd && uname` | denied: shell-синтаксис запрещён |
| `bash -c 'uname -a'` | denied: оболочка запрещена |
| `docker ps` | denied: этот профиль не управляет Docker хоста |
| `quit` | Выход и удаление контейнера |

Кавычки поддерживаются только для группировки аргументов. `cd`, конвейеры,
перенаправления, подстановка переменных и shell-скрипты здесь не выполняются.
API и ручная консоль вызывают один инструмент. Консоль преобразует строку
через `shlex.split`; MCP-клиент сразу передаёт массив аргументов.

Управляющие символы из вывода (ESC, C1, CR, bidi) консоль показывает как
`\x1b`, `\r`, `\u202e` и т. п., чтобы содержимое лога не управляло терминалом.
Обычный Unicode, emoji, переводы строк и табуляция сохраняются. Через MCP
возвращаются исходные данные в пределах лимита, без этого экранного экранирования.

### Самостоятельно проверить изоляцию и лимиты

Завершить обычную консоль командой `quit`. Для этих проверок запустить отдельную
консоль разработки: checker запрещает создание файлов и интерпретаторы, поэтому
здесь он явно отключается. Ограничения Docker и пользователь команды остаются.

```sh
bash executer/server_mcp/Run_docker.sh --manual --without-safety
```

Вводить по одной строке в `executor>`:

| Команда | Что проверить |
| --- | --- |
| `id` | uid=10001, gid=10001, пользователь executor |
| `pwd` | `/workspace` |
| `python3 -c "from pathlib import Path; p=Path('check.txt'); p.write_text('OK'); print(p.read_text(), oct(p.stat().st_mode & 0o777))"` | `OK 0o600`: запись разрешена в рабочей папке, права файла закрытые |
| `touch /app/should-fail` | failed: файловая система read-only |
| `ls /var/log/executor` | failed: журнал контроллера недоступен пользователю команды |
| `python3 -c "print('X'*9000)"` | Вывод усечён; сообщение `stdout truncated` |
| `sleep 40` | Примерно через 30 секунд status=timed_out |
| `python3 -c "print(chr(27)+'[2J')"` | Видимый текст `\x1b[2J`, экран не очищается |
| `quit` | Контейнер закрывается, временный check.txt удаляется вместе с /workspace |

К Codex подключать стандартный запуск ниже **без `--without-safety`**.

## 3. Подключить к Codex

Выполнить из корня проекта:

```sh
codex mcp add spas_executor -- /bin/bash "$(pwd)/executer/server_mcp/Run_docker.sh"
codex mcp list
```

Альтернатива — вручную добавить секцию в `~/.codex/config.toml`. Не нужно
делать оба варианта; при существующей секции отредактировать её:

```toml
[mcp_servers.spas_executor]
command = "/bin/bash"
args = ["/path/to/spas/executer/server_mcp/Run_docker.sh"]
startup_timeout_sec = 30
tool_timeout_sec = 45
enabled_tools = ["execute_command"]
```

На другой машине заменить абсолютный путь. Клиент должен иметь доступ к Docker.
Перезапустить MCP-подключение/сеанс; в Codex CLI проверить список через `/mcp`.
Формат конфигурации и CLI описаны в [официальной документации OpenAI](https://developers.openai.com/codex/mcp/).

Для этого MCP не нужны API-ключи Cloud.ru/OpenAI, OAuth или открытый порт:
Codex запускает локальный контейнер и общается с ним через stdio. Сам Codex
использует свою обычную авторизацию. Если команды `codex` нет в PATH, использовать
вариант с config.toml; не заменять существующий файл целиком.

Попросить Codex: «Вызови execute_command сервера spas_executor с command
[\"uname\", \"-s\"] и context: incident_id=demo-1, reason=проверка подключения».
Ожидаемый stdout — `Linux`, safety_checked=true. Команда выполняется в
контейнере Executor. Порт наружу не открывается; соединение — stdio.

Без `--manual` запуск выглядит «молча ожидающим»: сервер читает MCP-сообщения,
а не команды человека. Не добавлять `-t` к Docker: TTY нарушает stdio-протокол.
Конфигурация пользователя не меняется автоматически этими исходниками.

### Доступная функция MCP

Сервер публикует **один** инструмент — `execute_command(command, context)`.
Он принимает argv и контекст, запускает проверку и возвращает результат:

```json
{
  "command": ["uname", "-s"],
  "context": {"incident_id": "demo-1", "reason": "Проверка подключения"}
}
```

| Поле результата | Значение |
| --- | --- |
| `stdout`, `stderr` | Вывод, максимум 8192 байта на поток |
| `exit_code` | Код программы; null, если её не запускали |
| `status` | succeeded / failed / denied / approval_required / timed_out / error |
| `error` | Причина отказа или ошибки, либо null |
| `stdout_truncated`, `stderr_truncated` | Был ли вывод усечён |
| `safety_checked` | Получен ли корректный ответ checker |
| `request_id` | Идентификатор для поиска событий журнала |

По умолчанию разрешены `pwd`, поддержанные варианты `uname`, `ls /workspace`
и `ls /inputs`. `cat` требует конкретного пути в политике (раздел 4).
Обработка последовательная с момента проверки команды: при занятом сервере
приходит error с `Executor is busy` и `safety_checked=false`, без очереди к checker.
Это не обычный терминал: `cd`, pipe, редиректы и управление Docker хоста не предоставляются.

## 4. Разрешить чтение конкретного файла

Создать отдельную папку с данными, которые разрешено предоставить Executor.
Например, в терминале хоста из корня проекта:

```sh
mkdir -p /tmp/spas-demo-inputs
printf 'service is healthy\n' > /tmp/spas-demo-inputs/service.log
printf '%s\n' '{"readable_paths":["/workspace","/inputs","/inputs/service.log"]}' > /tmp/spas-demo-policy.json
chmod 755 /tmp/spas-demo-inputs
chmod 444 /tmp/spas-demo-inputs/service.log /tmp/spas-demo-policy.json
EXECUTOR_INPUTS_DIR=/tmp/spas-demo-inputs \
EXECUTOR_POLICY_FILE=/tmp/spas-demo-policy.json \
bash executer/server_mcp/Run_docker.sh --manual
```

Теперь `cat /inputs/service.log` разрешён. Host-папка монтируется readonly.
Доступны только **точные** пути политики, не все вложенные файлы каталога.
Симлинки и специальные файлы запрещены. Источник должен быть доступен на чтение
UID 10001; политика — контроллеру. Оператор не должен менять дерево входных
файлов во время запроса (см. ограничения в SERVER_MCP.md).

Для Codex с теми же ресурсами добавить в секцию сервера:

```toml
[mcp_servers.spas_executor.env]
EXECUTOR_INPUTS_DIR = "/tmp/spas-demo-inputs"
EXECUTOR_POLICY_FILE = "/tmp/spas-demo-policy.json"
```

Оба пути относятся к **хосту**; launcher передаёт политику внутрь контейнера
по фиксированному readonly-пути. Worker не может изменить политику через context.
Без конфигурации работают `pwd`, `uname`, `ls /workspace`, `ls /inputs`.

## 5. Автоматические проверки

Для тестов на хосте нужен Python 3.12+ с зависимостями из requirements.txt.
Можно использовать уже существующее окружение проекта или отдельное:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m executer.server_mcp.Smoke_test
```

Первая команда тестов проверяет checker, контракт, неизменность argv, отказы,
ошибки, отмену/тайм-аут checker, UTF-8 и ротацию журнала. Для проверки разрешения
на запуск Runner подменён: тесты не выполняют системные команды на хосте.

Вторая команда запускает **реальные** контейнеры и MCP stdio-сеансы: разрешённые
и запрещённые запросы, одновременные вызовы, readonly-входы, симлинки, UID/GID,
права записи, защиту контроллера, лимит вывода, тайм-аут 30 секунд, отмену
MCP-запроса, отказ при занятом Executor и удаление фонового setsid-потомка.
На время проверки ограничений Linux используется отдельный контейнер с
`--without-safety`, поскольку настоящий checker правильно запрещает команды
для создания файлов и интерпретаторы. Изоляция Docker при этом сохраняется.
Нужен заранее собранный образ. Весь smoke-тест ограничен 150 секундами.

Проверки также охватывают отображение управляющих символов в ручной консоли
и сохранение исходного содержимого через MCP, очистку сообщений самого SDK от
входных данных и восстановление после отмены проверки команды. Результат повторного запуска
тестов нужно оценивать по `OK` и коду завершения 0, а не по записи в документации.

В smoke-тесте используются временные входные файлы, сохраняются только журналы
в Docker volume. Полное описание ограничений: [SERVER_MCP.md](SERVER_MCP.md).

## 6. Посмотреть журнал

Текущий сеанс выводит JSON-события в stderr. Сохранённые журналы можно прочитать
отдельным одноразовым контейнером оператора с readonly-подключением volume:

```sh
docker run --rm --network none --read-only \
  --mount type=volume,src=spas-executor-logs,dst=/logs,readonly \
  --entrypoint python spas-executor:local -c \
  'from pathlib import Path; [print(p, p.read_text()[-4000:]) for p in Path("/logs").glob("*/*.log")]'
```

Для поиска конкретного запуска используй `request_id` из результата.
Журнал ротируется по 2 МиБ, сохраняет три резервных файла на процесс. Старые
каталоги контейнеров требуют отдельного срока хранения на стороне оператора.

## Если не запускается

- `Broken pipe` при `initialize`, а `command -v codex` показывает `/snap/bin/codex`:
  Snap-изоляция может закрыть дочерний Docker-процесс до MCP-рукопожатия.
  Использовать официальную Codex CLI, проверить `codex --version`, затем заново
  открыть Codex. Установка актуальной CLI описана в
  [официальной документации OpenAI](https://developers.openai.com/codex/cli/).
- `Cannot connect to Docker daemon` / `permission denied`: проверить доступ к Docker из того же пользователя, что запускает Codex.
- `image ... not found`: выполнить сборку и проверить тег `spas-executor:local`.
- `Protected Docker execution is required`: использовать Run_docker.sh, а не запускать Runner непосредственно на хосте.
- `denied`: посмотреть error; проверить точный путь и политику. Это штатный результат checker.
- Ошибка политики при старте: проверить JSON, права чтения, путь; сервер намеренно не переходит в режим без проверки.
