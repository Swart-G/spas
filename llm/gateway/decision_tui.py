"""One Decision test: mode, state, question, criteria, then the model's answer."""

import base64
import json
from pathlib import Path

from rich.table import Table
from rich.text import Text

from . import tui as ui
from .config import GatewayConfig
from .core.errors import GatewayError
from .core.system_one import SystemOneRequest
from .presentation import show_usage

MODES = {
    "choice": "Choice — выбрать один вариант",
    "noul": "Noul — оценить вероятность",
    "score": "Score — оценить по шкале",
    "box": "Box — найти объект на изображении",
}


def run_test(config_path: Path, state_dir: Path, backend: str):
    config = GatewayConfig.load(config_path)
    selected = config.backends[backend]
    if not selected.decision_model:
        raise GatewayError("Выберите подключение к модели решений.", status=400)
    mode = ui.menu(
        [(key, label) for key, label in MODES.items() if key != "box" or selected.supports_box],
        default="noul",
        title="Режим решения",
    )
    state = ui.ask("Состояние для оценки")
    if mode == "box":
        path = Path(ui.ask("Файл изображения")).expanduser()
        state = [state, image_part(path)]
    question = {"type": mode, "instructions": ui.ask("Вопрос")}
    if mode in {"choice", "score"}:
        label = "Варианты через ;" if mode == "choice" else "Уровни от низкого к высокому, через ;"
        values = [value.strip() for value in ui.ask(label).split(";")]
        if any(not value for value in values) or len(values) != len(set(values)):
            raise GatewayError(
                "Перечислите варианты через ;. Значения должны быть непустыми и не повторяться.",
                status=400,
            )
        if mode == "choice":
            question["criteria"] = dict.fromkeys(values)
        else:
            if not 2 <= len(values) <= 10:
                raise GatewayError("Укажите от 2 до 10 уровней шкалы.", status=400)
            question["criteria"] = values
    elif mode == "noul":
        criteria = {}
        for key, label in [("true", "Да"), ("false", "Нет")]:
            value = ui.ask(f"Условие «{label}» (можно пропустить)")
            if value.strip():
                criteria[key] = value
        if criteria:
            question["criteria"] = criteria
    request = SystemOneRequest(model=backend, state=state, questions={"result": question})
    ui.execute(
        config_path,
        state_dir,
        "decide",
        backend=backend,
        payload=request.model_dump(exclude_none=True),
        pretty=True,
    )


def image_part(path: Path):
    if path.stat().st_size > 10 * 1024 * 1024:
        raise GatewayError("Файл больше 10 МиБ. Выберите изображение меньшего размера.", status=400)
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise GatewayError(
            "Формат файла не поддерживается. Выберите PNG, JPEG или WebP.", status=400
        )
    return {
        "type": "image_url",
        "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(data).decode()},
    }


def show_result(result):
    if "answers" not in result:
        ui.console.print(json.dumps(result, ensure_ascii=False, indent=2), markup=False)
        return
    has_confidence = False
    for name, answer in result["answers"].items():
        kind = answer["type"]
        title = "Ответ модели" if name == "result" else name
        ui.console.print(Text(title, style="bold bright_cyan"))
        if kind == "refusal":
            ui.console.print("Модель отклонила вопрос.")
        elif kind == "noul":
            p = answer["noul"]
            ui.console.print(f"Вероятность «да»: {p:.1%}   «нет»: {1 - p:.1%}")
        elif kind == "box":
            box = answer["box"]
            table = Table("Граница объекта", "Координата")
            for key, label in [
                ("top", "Верхняя"),
                ("bottom", "Нижняя"),
                ("left", "Левая"),
                ("right", "Правая"),
            ]:
                table.add_row(label, f"{box[key]:.4f}")
            ui.console.print(table)
            ui.console.print("Координаты указаны в долях размера изображения: от 0 до 1.")
        else:
            value = answer["choice"] if kind == "choice" else f"{answer['score']:.2f}"
            ui.console.print(Text(f"{'Выбор' if kind == 'choice' else 'Оценка'}: {value}"))
            table = (
                Table("Вариант", "Вероятность")
                if kind == "choice"
                else Table("Уровень", "Описание", "Вероятность")
            )
            for key, p in answer["probabilities"].items():
                cells = [Text(key)]
                if kind == "score":
                    cells.append(Text(str((answer.get("legend") or {}).get(key, ""))))
                table.add_row(*cells, f"{p:.1%}")
            ui.console.print(table)
        if "confidence" in answer:
            has_confidence = True
            ui.console.print(f"Уверенность: {answer['confidence']:.1%}")
    if has_confidence:
        ui.console.print("Уверенность не означает вероятность правильного ответа.")
    if result.get("usage") is not None:
        show_usage(ui.console, result["usage"])
