# SAFETY CHECKER
Слой проверки безопасности выполняемых команд.

База Safety Analyzer находится в этом каталоге. Установка, запуск с заглушкой и
проверки описаны в [README.md](README.md). Предлагаемые входные/выходные контракты
команд версии `2.0 / command-1`: [docs/contracts.md](docs/contracts.md).
Rule-Based Checker обязателен. Модель может только дополнительно ограничить его
ALLOW, а DENY/REQUIRE_APPROVAL не доходят до LLM. Интеграция с Executor MCP:
[docs/executor.md](docs/executor.md).
