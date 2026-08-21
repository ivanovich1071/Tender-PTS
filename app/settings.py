"""Настройки приложения.

Живут в work/settings.json, правятся из интерфейса. Здесь только значения по
умолчанию и чтение-запись — никакой логики отбора.

Календарь праздников вынесен сюда намеренно: в Беларуси переносы рабочих дней
объявляются постановлением на каждый год, и оператор должен править список
текстом, не трогая код.

Ключи — отдельно от настроек, в `.env` в корне проекта. Файл в git не попадает
(см. `.gitignore`), в журнал не пишется и в сборку не кладётся. Это привычное
место: его правят в редакторе, не запуская приложение.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# В собранном .exe исходники распакованы во временную папку, и складывать туда
# базу, журналы и ключ нельзя — при следующем запуске папка будет другая.
# Поэтому рабочие файлы всегда лежат рядом с самим .exe.
FROZEN = bool(getattr(sys, "frozen", False))
ROOT = (Path(sys.executable).resolve().parent if FROZEN
        else Path(__file__).resolve().parent.parent)
BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))       # что вшито в сборку

FILE = ROOT / "work" / "settings.json"
ENV_FILE = ROOT / ".env"

ENV_TEMPLATE = """# Ключи Tender-PTS. Файл в git не попадает и в сборку не кладётся.
# Ключ OpenRouter — для отбора лотов моделью. Без него отбор идёт на правилах.
# Взять на https://openrouter.ai/keys
OPENROUTER_API_KEY=
"""


def load_env() -> None:
    """Прочитать .env в переменные окружения. Заведёт пустой, если его нет.

    Разбор нарочно свой и в десять строк: тянуть зависимость ради `KEY=value`
    незачем, а лишний пакет в сборке — лишний вес.
    """
    if not ENV_FILE.exists():
        try:
            ENV_FILE.write_text(ENV_TEMPLATE, encoding="utf-8")
        except OSError:
            return                      # только для чтения — не беда, читаем окружение
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name and value and not os.environ.get(name):
            os.environ[name] = value


load_env()

DEFAULTS: dict = {
    # Сбор
    "window_days": 30,          # насколько глубоко листаем выдачу поиска
    "min_working_days": 2,      # минимальный запас до окончания подачи
    "sources": {"gias": True, "butb": False, "icetrade": True},
    "proxy": "",                # пусто = без прокси; напр. socks5://user:pass@host:port
    "request_pause": 0.4,       # пауза между запросами к площадке, сек

    # icetrade.by закрыт для адресов вне Беларуси — без прокси источник
    # отвечает 403. Разметка снята с настоящих страниц и проверяется тестами
    # на tests/fixtures/icetrade, поэтому селекторов в настройках нет: они
    # живут в коде вместе с тестами, а здесь только то, что оператор меняет.
    "icetrade": {
        "base": "https://icetrade.by",
        "allow_insecure_tls": True,
        # Окно по сроку подачи, а не по дате размещения: закупка Гомельского
        # химического завода размещена 25.06, а предложения принимает до 31.08.
        "deadline_window_days": 90,
        "on_page": 100,
        "max_pages": 20,        # предел листания одной выдачи
        "max_cards": 400,       # предел карточек за прогон
        "industry_match": ["машиностроен", "металлург", "химическ"],
        "industries": [],       # идентификаторы отраслей, если выбраны вручную
        "keyword_pass": True,   # проход по словам профиля: ради редких сплавов
    },

    # Оценка $/кг: пороги по группам работ, доллары за килограмм.
    # green — дешевле или равно, red — дороже или равно, между ними жёлтый.
    "price_thresholds": {
        "штамповка": {"green": 4.0, "red": 8.0},
        "литьё": {"green": 3.0, "red": 6.0},
        "механообработка": {"green": 8.0, "red": 20.0},
        "по умолчанию": {"green": 5.0, "red": 12.0},
    },

    # Модель-судья: второй уровень отбора. Отличает поддон печи закалочной от
    # поддона деревянного — правилами это не берётся. Идёт через OpenRouter:
    # один ключ, модель меняется строкой. Ключ лежит в work/settings.json,
    # который в git не попадает, и в журнале маскируется.
    "judge": True,              # выключить — отбор останется на правилах
    "openrouter_key": "",
    "model": "nvidia/nemotron-3-super-120b-a12b:free",
    "judge_batch": 20,          # лотов в одном запросе к модели

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
    # Настройки площадок — чисто машинные, и устаревший ключ здесь опаснее
    # мусора: увидев в файле забытый search_path, его начнут править, а он ни
    # на что не влияет. Пороги оценки не чистим — там группы добавляет человек.
    data["icetrade"] = {k: v for k, v in data["icetrade"].items()
                        if k in DEFAULTS["icetrade"]}
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
