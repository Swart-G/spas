# CONTRACTS
Строгие структуры, которыми модели обмениваются между собой.

## Safety (предварительный контракт)

`safety.py`: `CommandRequest`, `SafetyResult`, `Decision`, `RiskLevel`.
Контракт используется rule-based Safety Checker; интеграцию с Executor MCP
нужно согласовать при реализации сервера. Подробности и ограничения:
[Safety Checker](../../executer/safety_checker/SAFETY_CHECKER.md).
