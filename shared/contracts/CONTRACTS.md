# CONTRACTS
Строгие структуры, которыми модели обмениваются между собой.

## Safety и порт Executor MCP

`safety.py`: `CommandRequest`, `SafetyResult`, `Decision`, `RiskLevel` — внутренний
контракт rule-based checker. `ExecutionContextPort`, `ExecutorSafetyDecision` и
`ExecutorSafetyCheckerPort` — порт совместимости с текущим Executor MCP.

Executor вызывает `async check(command: str, context)`; контекст содержит
`incident_id` и `reason`. Ответ содержит ровно два поля, например
`{"decision": "DENY", "reason": "..."}`. Допустимые решения: `ALLOW`, `DENY`,
`REQUIRE_APPROVAL`.
Адаптер `ExecutorSafetyChecker` не передаёт внутренние поля риска/argv в этот
словарь: текущий валидатор Executor запрещает дополнительные поля.

Совместимость проверена с `workeragent/executermcp` на коммите `79f4fd0`.
Утверждение общего контракта командой остаётся шагом ревью. Подробности и ограничения:
[Safety Checker](../../executer/safety_checker/SAFETY_CHECKER.md).
