"""High contrast, Compose-style progress for a full managed Colab startup."""

import time
from dataclasses import dataclass

from rich.console import Group
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

STEPS = {
    "allocate": "Colab runtime · выделение или восстановление",
    "connect": "Colab proxy · подключение",
    "kernel": "Jupyter · запуск установщика",
    "prepare": "Runtime · подготовка и проверка кэша",
    "stop_previous": "Предыдущая модель · остановка",
    "venv": "Python · окружение",
    "dependencies": "Python · зависимости сервера",
    "torch_check": "PyTorch · проверка",
    "torch": "PyTorch · установка",
    "model_download": "Модель · скачивание весов и tokenizer",
    "nimble_prepare": "Nimble · подготовка модели решений",
    "scorer_download": "Nimble · скачивание scorer",
    "contract": "Nimble · проверка контракта",
    "base_download": "Qwen · скачивание базовых весов",
    "merge": "Nimble · слияние LoRA",
    "server": "Сервер · запуск и загрузка модели",
    "verify": "Gateway · проверочный запрос к модели",
    "route": "Gateway · применение маршрута",
}
STATUS = {
    "pending": ("○", "Ожидание", "bright_white"),
    "running": ("⠿", "Выполняется", "bright_cyan"),
    "retrying": ("↻", "Повтор", "bright_yellow"),
    "done": ("✔", "Готово", "bright_green"),
    "skipped": ("─", "Пропущено", "bright_white"),
    "failed": ("✘", "Ошибка", "bold bright_red"),
    "cancelled": ("!", "Прервано", "bright_yellow"),
}


REASONS = {
    "stalled": "Застой скачивания",
    "network_error": "Временная ошибка сети",
    "rate_limited": "Ограничение сервера",
    "worker_failed": "Загрузчик завершился без результата",
    "permanent_error": "Ошибка доступа, модели или локального хранилища",
}


def valid_progress(event):
    """Only fixed labels and bounded counters may leave the remote setup process."""
    if (
        not isinstance(event, dict)
        or not isinstance(event.get("step"), str)
        or event["step"] not in STEPS
    ):
        return None
    if (
        not isinstance(event.get("status"), str)
        or event["status"] not in STATUS
        or event["status"] == "pending"
    ):
        return None
    clean = {"type": "progress", "step": event["step"], "status": event["status"]}
    for key in (
        "completed",
        "total",
        "files_completed",
        "files_total",
        "vm_started_at",
        "vm_lifetime_seconds",
    ):
        value = event.get(key)
        if type(value) in (int, float) and 0 <= value <= 10**15:
            clean[key] = value
    if isinstance(event.get("unit"), str) and event["unit"] in {"bytes", "files"}:
        clean["unit"] = event["unit"]
    for key in ("attempt", "max_attempts"):
        if type(event.get(key)) is int and 1 <= event[key] <= 10:
            clean[key] = event[key]
    if type(event.get("retry_delay")) in (int, float) and 0 <= event["retry_delay"] <= 60:
        clean["retry_delay"] = event["retry_delay"]
    if isinstance(event.get("reason"), str) and event["reason"] in REASONS:
        clean["reason"] = event["reason"]
    return clean


@dataclass
class Step:
    status: str = "pending"
    started: float | None = None
    ended: float | None = None
    completed: float = 0
    total: float = 0
    unit: str = "files"
    files_completed: float = 0
    files_total: float = 0
    sample_time: float | None = None
    sample_completed: float = 0
    rate: float = 0
    attempt: int = 0
    max_attempts: int = 0
    reason: str = ""
    retry_delay: float = 0
    retry_started: float | None = None


def amount(value):
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or suffix == "TiB":
            return f"{value:.1f} {suffix}"
        value /= 1024


class StartupDisplay:
    def __init__(self, console, name, model, *, nimble=False, clock=time.monotonic):
        self.console, self.name, self.model, self.clock = console, name, model, clock
        self.started = clock()
        self.ended = None
        self.result = "Запуск"
        self.vm_started_at = None
        self.vm_lifetime_seconds = None
        nimble_steps = {"nimble_prepare", "scorer_download", "contract", "base_download", "merge"}
        self.rows = {key: Step() for key in STEPS if nimble or key not in nimble_steps}
        self.live = Live(
            console=console,
            get_renderable=self.render,
            refresh_per_second=4,
            transient=False,
            vertical_overflow="ellipsis",
        )

    def update(self, event):
        event = valid_progress(event)
        if event is None or event["step"] not in self.rows:
            return
        now = self.clock()
        row = self.rows[event["step"]]
        if "vm_started_at" in event:
            self.vm_started_at = event["vm_started_at"]
        if "vm_lifetime_seconds" in event:
            self.vm_lifetime_seconds = event["vm_lifetime_seconds"]
        if row.started is None and event["status"] != "skipped":
            row.started = now
        row.status = event["status"]
        if row.status in {"running", "retrying"}:
            row.ended = None
        if (
            event.get("unit", row.unit) != row.unit
            or event.get("attempt", row.attempt) != row.attempt
        ):
            row.sample_time = None
            row.rate = 0
        if row.status == "retrying":
            row.retry_started = now
        elif row.status == "running":
            row.reason = ""
        for key in (
            "completed",
            "total",
            "unit",
            "files_completed",
            "files_total",
            "attempt",
            "max_attempts",
            "reason",
            "retry_delay",
        ):
            if key in event:
                setattr(row, key, event[key])
        if row.sample_time is not None and now - row.sample_time >= 0.2:
            row.rate = max(0, (row.completed - row.sample_completed) / (now - row.sample_time))
            row.sample_time, row.sample_completed = now, row.completed
        elif row.sample_time is None:
            row.sample_time, row.sample_completed = now, row.completed
        if row.status not in {"running", "retrying"}:
            row.ended = now

    def render(self):
        now = self.ended if self.ended is not None else self.clock()
        completed = sum(row.status in {"done", "skipped"} for row in self.rows.values())
        heading = Text(
            f"[+] {self.result} {completed}/{len(self.rows)} · {now - self.started:.1f}s",
            style="bold bright_white",
        )
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(width=2)
        table.add_column(ratio=1)
        table.add_column(width=13)
        table.add_column(width=10, justify="right")
        for key, row in self.rows.items():
            icon, label, color = STATUS[row.status]
            duration = (
                f"{(row.ended if row.ended is not None else now) - row.started:.1f}s"
                if row.started is not None
                else "—"
            )
            table.add_row(
                Text(icon, style=color),
                Text(STEPS[key], style="bright_white"),
                Text(label, style=color),
                Text(duration, style="bright_white"),
            )
            if row.attempt:
                detail = f"Попытка {row.attempt}/{row.max_attempts}"
                if row.reason:
                    detail += " · " + REASONS[row.reason]
                if row.status == "retrying":
                    remaining = max(0, row.retry_delay - (now - row.retry_started))
                    detail += f" · повтор через {remaining:.0f}s"
                table.add_row("", Text(detail, style="bright_yellow"), "", "")
            if row.total > 0:
                fraction = min(row.completed / row.total, 1)
                detail = (
                    f"{amount(row.completed)} / {amount(row.total)}"
                    if row.unit == "bytes"
                    else f"{int(row.completed)}/{int(row.total)} файлов"
                )
                if row.unit == "bytes" and row.files_total:
                    detail += f" · файлов {int(row.files_completed)}/{int(row.files_total)}"
                if row.unit == "bytes" and row.status == "running":
                    detail += f" · {amount(row.rate)}/s"
                    if row.rate > 0:
                        detail += (
                            f" · осталось ~{max(0, row.total - row.completed) / row.rate:.0f}s"
                        )
                if row.status == "running":
                    table.add_row(
                        "",
                        ProgressBar(
                            total=row.total,
                            completed=row.completed,
                            complete_style="bright_cyan",
                            finished_style="bright_green",
                        ),
                        Text(f"{fraction:.0%}", style="bright_white"),
                        "",
                    )
                table.add_row("", Text(detail, style="bright_white"), "", "")
        info = f"{self.name} · {self.model}"
        if self.vm_started_at and self.vm_lifetime_seconds:
            remaining = max(0, int(self.vm_lifetime_seconds - (time.time() - self.vm_started_at)))
            info += f" · VM: ≈ {remaining // 3600}ч {(remaining % 3600) // 60}м осталось (оценка)"
        return Group(heading, Text(info, style="bright_cyan"), table)

    def __enter__(self):
        self.live.start(refresh=True)
        return self

    def __exit__(self, kind, error, traceback):
        self.ended = self.clock()
        self.result = (
            "Готово"
            if kind is None
            else "Прервано"
            if issubclass(kind, BaseException)
            and (kind.__name__ in {"CancelledError", "KeyboardInterrupt"})
            else "Ошибка"
        )
        for row in self.rows.values():
            if row.status in {"running", "retrying"}:
                row.status = (
                    "done"
                    if kind is None
                    else "cancelled"
                    if self.result == "Прервано"
                    else "failed"
                )
                row.ended = self.ended
            elif row.status == "pending" and kind is None:
                row.status = "skipped"
        self.live.stop()
