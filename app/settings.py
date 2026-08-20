"""Настройки приложения.

Живут в work/settings.json, правятся из интерфейса. Здесь только значения по
умолчанию и чтение-запись — никакой логики отбора.

Календарь праздников вынесен сюда намеренно: в Беларуси переносы рабочих дней
объявляются постановлением на каждый год, и оператор должен править список
текстом, не трогая код.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "work" / "settings.json"

DEFAULTS: dict = {
    # Сбор
    "window_days": 30,          # насколько глубоко листаем выдачу поиска
    "min_working_days": 2,      # минимальный запас до окончания подачи
    "sources": {"gias": True, "butb": False, "icetrade": False},
    "proxy": "",                # пусто = без прокси; напр. socks5://user:pass@host:port
    "request_pause": 0.4,       # пауза между запросами к площадке, сек

    # Оценка $/кг: пороги по группам работ, доллары за килограмм.
    # green — дешевле или равно, red — дороже или равно, между ними жёлтый.
    "price_thresholds": {
        "штамповка": {"green": 4.0, "red": 8.0},
        "литьё": {"green": 3.0, "red": 6.0},
        "механообработка": {"green": 8.0, "red": 20.0},
        "по умолчанию": {"green": 5.0, "red": 12.0},
    },

    # LLM (нужен с шага 3)
    "model": "qwen/qwen3.7-flash",

    # Праздники и переносы РБ. Формат YYYY-MM-DD.
    # Список на 2026 год — проверить и дополнить по постановлению Совмина.
    "holidays": [
        "2026-01-01", "2026-01-02", "2026-01-07",
        "2026-03-09", "2026-05-01", "2026-05-09",
        "2026-07-03", "2026-11-07", "2026-12-25",
    ],
    # Рабочие субботы (переносы) — в эти дни срок считается рабочим днём.
    "working_weekends": [],
}


def read() -> dict:
    data = json.loads(json.dumps(DEFAULTS))  # глубокая копия
    if FILE.exists():
        try:
            stored = json.loads(FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return data
        for key, value in stored.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key].update(value)
            else:
                data[key] = value
    return data


def write(values: dict) -> dict:
    data = read()
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def work_dir() -> Path:
    folder = ROOT / "work"
    folder.mkdir(parents=True, exist_ok=True)
    return folder
