"""Shared vocabulary and human-readable views for the terminal interface."""

from datetime import UTC, datetime

from rich.table import Table
from rich.text import Text

AUTH_STATES = {
    "CONNECTED": "Доступ настроен",
    "DISCONNECTED": "Нет подключения",
    "EXPIRED": "Сессия истекла",
    "RATE_LIMITED": "Лимит исчерпан",
    "REAUTH_REQUIRED": "Обновите авторизацию",
}
PROVIDERS = {
    "codex_subscription": "Подписка ChatGPT",
    "claude_subscription": "Подписка Claude",
    "openai_api": "OpenAI API",
    "openai_compatible": "Совместимый API",
    "google_colab": "Google Colab",
    "colab_managed": "Модель в Colab",
    "remote_inference": "Сервер модели",
}
FORMATS = {
    "chat_completions": "Chat Completions",
    "responses": "Responses",
    "decisions": "Decisions",
    "systemone": "System One",
}
STAGES = {
    "CONFIGURED": "Готово к запуску",
    "ALLOCATING": "Создаётся среда Colab",
    "INSTALLING": "Устанавливается сервер",
    "DOWNLOADING": "Скачивается модель",
    "STARTING": "Загружается модель",
    "VERIFYING": "Проверяется ответ модели",
    "VERIFIED": "Проверка пройдена",
    "READY": "Модель готова",
    "STOPPED": "Модель остановлена",
    "DELETED": "Среда удалена",
    "FAILED": "Ошибка запуска",
    "EXPIRED": "Среда завершила работу",
    "INTERRUPTED": "Запуск отменён",
}
RESULTS = {
    "completed": "Готово",
    "cancelled": "Отменено",
    "rate_limited": "Лимит исчерпан",
    "provider_unavailable": "Сервис недоступен",
    "provider_timeout": "Время ожидания истекло",
    "provider_incomplete": "Неполный ответ",
    "reauth_required": "Обновите авторизацию",
    "provider_access_denied": "Доступ отклонён",
    "provider_invalid_request": "Сервис отклонил запрос",
    "provider_payment_required": "Недостаточно средств",
    "unsupported_parameter": "Неподдерживаемый запрос",
}
RETURN_PROMPT = "Enter — вернуться"


def safe_terminal_text(value: str) -> str:
    """Keep model text from executing terminal controls while preserving line breaks."""
    return "".join(
        f"\\u{ord(char):04x}"
        if 0xD800 <= ord(char) <= 0xDFFF
        else f"\\x{ord(char):02x}"
        if (ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) < 160
        else char
        for char in value
    )


def show_usage(console, usage):
    labels = {
        "prompt_tokens": "На входе",
        "input_tokens": "На входе",
        "completion_tokens": "В ответе",
        "output_tokens": "В ответе",
        "total_tokens": "Всего",
    }
    values = {}
    for key, label in labels.items():
        if usage.get(key) is not None:
            values[label] = str(usage[key])
    if values:
        console.print(Text("Токены: " + " · ".join(f"{v} {k.lower()}" for k, v in values.items())))


def show_models(console, models, *, decision=False):
    if not models:
        console.print("В каталоге пока нет моделей. ID модели можно указать вручную.")
        return
    table = Table("ID модели", "Название", "Тип")
    for column in table.columns:
        column.overflow = "fold"
    for model in models:
        kind = model.get("kind")
        table.add_row(
            Text(model["id"]),
            Text(model.get("name", model["id"])),
            "Решения" if kind == "decision" or decision else "Чат",
        )
    console.print(table)


def show_status(console, data):
    table = Table("Параметр", "Состояние")

    def row(label, value):
        if value is not None:
            table.add_row(Text(label), Text(str(value)))

    row("Аккаунт", data.get("account"))
    row("Сервис", PROVIDERS.get(data.get("provider")))
    row("Авторизация", AUTH_STATES.get(data.get("state"), "Нет данных"))
    if "available" in data:
        row("Доступность сервиса", "Доступен" if data["available"] else "Недоступен")
    if "model_available" in data:
        row("Модель в каталоге", "Есть" if data["model_available"] else "Не найдена")
    row("Модель", data.get("model_repo"))
    row("Среда Colab", STAGES.get(data.get("stage")))
    vm = data.get("vm_time") or {}
    remaining = vm.get("remaining_seconds")
    if remaining is not None:
        row("Осталось времени (оценка)", f"{remaining // 3600} ч {(remaining % 3600) // 60} мин")
    if "auto_creation" in data:
        auto = data["auto_creation"]
        if isinstance(auto, dict):
            row(
                "Автозапуск",
                "Приостановлен"
                if auto.get("paused")
                else "Включён"
                if auto.get("enabled")
                else "Выключен",
            )
    success = data.get("last_success_at")
    if isinstance(success, (int, float)):
        row(
            "Последний успешный запрос",
            datetime.fromtimestamp(success, UTC).strftime("%d.%m.%Y %H:%M UTC"),
        )
    code = data.get("code") or data.get("last_error")
    if code:
        row("Результат проверки", RESULTS.get(code, "Не удалось завершить проверку"))
    console.print(table)
