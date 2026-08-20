"""Журнал работы в отдельный файл.

Приложение запускают в одном месте, а разбираются в том, что случилось, — в
другом. Экран с бегущим прогрессом переслать нельзя, файл — можно, поэтому всё,
что видит оператор, и всё, чего он не видит, пишется в `work/logs/`.

Файл заводится один на запуск приложения и назван по времени старта. В начале
каждого файла — обстановка: версия Python, есть ли прокси, какие площадки
включены, какой профиль. Без этого удалённый разбор превращается в переписку
«а что у вас в настройках».

**Пароль прокси в журнал не попадает.** Строка вида
`socks5://user:pass@host:port` пишется как `socks5://***@host:port`: журнал
пересылают в мессенджерах, и утечка доступа отсюда была бы глупой.
"""
from __future__ import annotations

import logging
import platform
import re
import sys
from datetime import datetime
from pathlib import Path

from app import settings

DIR = settings.ROOT / "work" / "logs"
_file: Path | None = None

log = logging.getLogger("tender")

PROXY_CREDENTIALS = re.compile(r"://[^/@\s]+@")


def mask_proxy(proxy: str) -> str:
    """Прокси без логина и пароля — журнал пересылают посторонним."""
    if not proxy:
        return "(не задан)"
    return PROXY_CREDENTIALS.sub("://***@", proxy)


def path() -> Path | None:
    return _file


def setup() -> Path:
    """Завести файл журнала на этот запуск и записать обстановку."""
    global _file
    if _file:
        return _file

    DIR.mkdir(parents=True, exist_ok=True)
    _file = DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}.log"

    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                            datefmt="%H:%M:%S")
    to_file = logging.FileHandler(_file, encoding="utf-8")
    to_file.setFormatter(fmt)
    to_screen = logging.StreamHandler(sys.stderr)
    to_screen.setFormatter(fmt)

    log.setLevel(logging.INFO)
    log.handlers = [to_file, to_screen]
    log.propagate = False

    cfg = settings.read()
    prof_version = "?"
    try:
        from app import profile as profile_mod
        prof_version = profile_mod.load().raw.get("version", "?")
    except Exception as e:                       # профиль сломан — это тоже новость
        prof_version = f"не прочитан: {type(e).__name__}"

    enabled = [name for name, on in (cfg.get("sources") or {}).items() if on]
    log.info("=" * 62)
    log.info("Tender-PTS запущен")
    log.info("Python %s на %s", platform.python_version(), platform.platform())
    log.info("прокси: %s", mask_proxy(cfg.get("proxy", "")))
    log.info("площадки: %s", ", ".join(enabled) or "ни одной")
    log.info("профиль: версия %s", prof_version)
    log.info("окно по сроку подачи: %s дн., запас не меньше %s раб. дн.",
             (cfg.get("icetrade") or {}).get("deadline_window_days"),
             cfg.get("min_working_days"))
    log.info("журнал: %s", _file)
    log.info("=" * 62)
    return _file


def run_result(stats: dict) -> None:
    """Итог сбора: сколько нашли, что сломалось, о чём предупредили."""
    for name, source in (stats.get("sources") or {}).items():
        log.info("итог %s: закупок %s, с подходящими лотами %s",
                 name, source.get("actual"), source.get("saved"))
    log.info("итог: лотов %s, закупок %s, мимо профиля %s, запросов %s, дублей %s",
             stats.get("saved_lots"), stats.get("saved_purchases"),
             stats.get("no_match"), stats.get("calls"), stats.get("duplicates"))
    for warning in stats.get("warnings") or []:
        log.warning(warning)
    for error in stats.get("errors") or []:
        log.error(error)
