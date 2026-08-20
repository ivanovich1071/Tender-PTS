"""Общий интерфейс источника закупок.

Источники устроены по-разному: у ГИАС есть JSON API, у icetrade и БУТБ его нет
и придётся разбирать HTML. Общего у них ровно одно — они отдают закупки, лоты и
ссылки на документацию в одинаковой форме. Всё остальное (отбор по профилю,
фильтр актуальности, запись) живёт в collector.py и одинаково для всех.

Источник обязан отфильтровать по сроку подачи сам: у каждой площадки свои
названия состояний, и знать их — его дело, а не collector.py.
"""
from __future__ import annotations

from typing import Iterable, Protocol

# Что отдаёт источник: (закупка, её лоты, её файлы) — ключи совпадают с колонками
# таблиц purchases / lots / files в store.py.
Harvest = Iterable[tuple[dict, list[dict], list[dict]]]


class SourceError(RuntimeError):
    """Площадка недоступна или ответила не тем. Сбор по другим источникам продолжается."""


class Source(Protocol):
    name: str
    title: str

    def harvest(self, profile, cfg: dict, progress=None, cancelled=None) -> Harvest:
        ...
