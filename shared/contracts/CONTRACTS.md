# CONTRACTS

Структуры данных, которыми компоненты обмениваются между собой.

## Safety Checker

`ExecutorSafetyCheckerPort`: `async check(command: list[str], context)`.
Контекст содержит только `incident_id` и `reason`; ответ — `dict[str, object]`
с полями `decision` (`ALLOW`, `DENY`, `REQUIRE_APPROVAL`) и `reason` (строка).
`ExecutorSafetyDecision` — внутренний `TypedDict`, не возвращаемый тип метода.
`SafetyResult`, `Decision` и `RiskLevel` используются внутри правил.

Роль агента не передаётся в контексте. Параметр `agent_role` при создании
сервера принимает `AgentRole.DIAGNOSTIC` или `AgentRole.WORKER`. Его задаёт доверенное
приложение; одна роль на сервер, по умолчанию — Diagnostic.

[Правила, подключение и тесты](../../executer/safety_checker/SAFETY_CHECKER.md).
