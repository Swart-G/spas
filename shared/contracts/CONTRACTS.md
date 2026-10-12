# CONTRACTS

Структуры данных, которыми компоненты обмениваются между собой.

## Safety Checker

`ExecutorSafetyCheckerPort`: `async check(command: list[str], context)`.
Контекст содержит `incident_id` и `reason`; ответ — `dict[str, object]` с двумя
полями: `decision` (`ALLOW`, `DENY`, `REQUIRE_APPROVAL`) и `reason` (строка).
`ExecutorSafetyDecision` — внутреннее описание этих полей через `TypedDict`.

`SafetyResult`, `Decision` и `RiskLevel` используются внутри правил. Поля риска,
правила и argv в ответ Executor не входят. Строки команд запрещены.

На стороне Executor остаётся заменить строковый вход и запуск через Bash на
`list[str]` и `create_subprocess_exec(*argv)`. Формат ответа менять не нужно.
[Правила, подключение и проверки](../../executer/safety_checker/SAFETY_CHECKER.md).
