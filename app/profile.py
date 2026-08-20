"""Профиль компании: номенклатура, стоп-слова, организаторы под наблюдением.

Профиль лежит в profile.json в корне проекта и правится текстом — это словарь
предметной области, он будет меняться чаще кода.

Здесь же транслитерация. Она нужна вот зачем: площадка переименовывает
приложенные файлы в транслит («shesternya-56.jpg», «val-shesternya-97.jpg»),
и по одному имени файла видно, что за деталь, — не скачивая и не открывая его.
Схема подобрана по реальным именам с goszakupki.by.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_FILE = ROOT / "profile.json"

# Порядок важен: двухбуквенные сочетания идут раньше одиночных букв.
TRANSLIT = [
    ("щ", "sch"), ("ш", "sh"), ("ч", "ch"), ("ж", "zh"), ("ю", "yu"), ("я", "ya"),
    ("ё", "e"), ("э", "e"), ("ц", "c"), ("х", "h"), ("у", "u"), ("ы", "y"),
    ("а", "a"), ("б", "b"), ("в", "v"), ("г", "g"), ("д", "d"), ("е", "e"),
    ("з", "z"), ("и", "i"), ("й", "i"), ("к", "k"), ("л", "l"), ("м", "m"),
    ("н", "n"), ("о", "o"), ("п", "p"), ("р", "r"), ("с", "s"), ("т", "t"),
    ("ф", "f"), ("ь", ""), ("ъ", ""),
]


def norm(text: str | None) -> str:
    """К нижнему регистру, всё кроме букв и цифр — в пробел."""
    return re.sub(r"[^а-яёa-z0-9]+", " ", (text or "").lower()).strip()


@lru_cache(maxsize=4096)
def translit(word: str) -> str:
    out = word.lower()
    for cyr, lat in TRANSLIT:
        out = out.replace(cyr, lat)
    return out


def stem(token: str) -> str:
    """Грубое отсечение окончания.

    Полноценный стеммер здесь избыточен: слова предметные, а окончательное
    решение об омонимах всё равно принимает LLM-уровень и оператор.
    """
    return token[:-2] if len(token) > 5 else token


@dataclass
class Keyword:
    text: str
    group: str
    tokens: tuple[str, ...]
    lat_tokens: tuple[str, ...]

    def hits(self, haystack: str) -> bool:
        return all(t in haystack for t in self.tokens)

    def hits_latin(self, haystack: str, words: frozenset[str]) -> bool:
        """Совпадение по имени файла. Нужны все части ключа, а не какая-то одна.

        Короткие основы вроде «val» ищутся только как отдельное слово: в
        транслите они складываются со случайными кусками («valenki»), а если
        просто выбрасывать их, то «вал шестерня» начинает срабатывать на любом
        файле с шестернёй.
        """
        for token in self.lat_tokens:
            if len(token) >= 5:
                if token not in haystack:
                    return False
            elif not any(w == token or w.startswith(token) for w in words):
                return False
        return True


@dataclass
class Profile:
    raw: dict
    keywords: list[Keyword] = field(default_factory=list)
    stop_words: list[str] = field(default_factory=list)
    service_markers: list[str] = field(default_factory=list)
    supply_markers: list[str] = field(default_factory=list)
    organizers: list[dict] = field(default_factory=list)

    @property
    def groups(self) -> dict[str, list[str]]:
        return self.raw.get("groups", {})

    def queries(self) -> list[tuple[str, str]]:
        """Пары (ключевое слово, группа) для поиска на площадке."""
        return [(k.text, k.group) for k in self.keywords]


def load(path: Path | None = None) -> Profile:
    data = json.loads((path or PROFILE_FILE).read_text(encoding="utf-8"))
    keywords: list[Keyword] = []
    for group, words in data.get("groups", {}).items():
        for word in words:
            tokens = tuple(stem(t) for t in norm(word).split() if t)
            if not tokens:
                continue
            keywords.append(Keyword(
                text=word, group=group, tokens=tokens,
                lat_tokens=tuple(translit(t) for t in tokens),
            ))
    return Profile(
        raw=data,
        keywords=keywords,
        stop_words=[w.lower() for w in data.get("stop_words", [])],
        service_markers=[w.lower() for w in data.get("service_markers", [])],
        supply_markers=[w.lower() for w in data.get("supply_markers", [])],
        organizers=data.get("organizers_watch", []),
    )
