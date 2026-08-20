"""Отбор лотов по профилю компании.

Первый уровень, без модели. Считает две вещи:

1. **Подходит ли лот по номенклатуре.** Смотрим название лота, коды ОКПБ и
   имена приложенных файлов. Имя файла — самостоятельный сигнал: площадка
   переименовывает вложения в транслит («shesternya-56.jpg»), и по нему видно
   деталь, даже когда лот назван «Аукцион» или «Закупка из одного источника».
   Разведка показала, что самые крупные лоты названы именно так.

2. **Поставка это или работа.** ПТС поставляет изделия, а не выполняет ремонт
   и промывку. Услуг в выдаче больше, чем поставок, поэтому они отсекаются
   отдельным признаком, а не стоп-словами.

Смысловой отсев омонимов (медицинский теплообменник, магнитная мешалка,
шестерня на трактор) — задача LLM-уровня, он появится позже.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.profile import Profile, norm


@dataclass
class Match:
    matched: bool
    kind: str          # supply | service | stop
    group: str
    keywords: list[str]
    reason: str

    def as_row(self) -> dict:
        return {
            "kind": self.kind,
            "grp": self.group,
            "keywords": "; ".join(self.keywords),
            "reason": self.reason,
        }


def classify_kind(profile: Profile, text: str) -> str:
    low = text.lower()
    if any(w in low for w in profile.stop_words):
        return "stop"
    # «Поставка и монтаж» — всё-таки поставка, поэтому маркер поставки сильнее.
    if any(m in low for m in profile.supply_markers):
        return "supply"
    if any(m in low for m in profile.service_markers):
        return "service"
    return "supply"


def match_lot(profile: Profile, lot_title: str, purchase_title: str = "",
              okpb: str = "", filenames: list[str] | None = None) -> Match:
    haystack = norm(f"{lot_title} {okpb}")
    files_hay = norm(" ".join(filenames or []))
    files_words = frozenset(files_hay.split())

    hits: list[str] = []
    groups: list[str] = []
    only_by_file = True
    for kw in profile.keywords:
        if kw.hits(haystack):
            hits.append(kw.text)
            groups.append(kw.group)
            only_by_file = False
        elif files_hay and kw.hits_latin(files_hay, files_words):
            hits.append(kw.text)
            groups.append(kw.group)

    if not hits:
        return Match(False, "", "", [], "")

    kind = classify_kind(profile, f"{lot_title} {purchase_title}")
    reason = "имя приложенного файла" if only_by_file else "название лота"
    if kind == "service":
        reason = "похоже на работу или услугу, не поставку"
    elif kind == "stop":
        reason = "стоп-слово профиля"
    return Match(True, kind, groups[0], hits, reason)


def match_by_organizer(profile: Profile, organizer: str,
                       unp: str | None = None) -> dict | None:
    """Заказчик из списка наблюдения. Такие лоты берём независимо от слов.

    Сначала по УНП, и только потом по названию. Название на площадке набирают
    руками: БМЗ на icetrade значится как «Белорусский металлургичекий завод» —
    с опечаткой, и совпадение по строке «белорусский металлургический» её не
    ловит. УНП же не меняется и переживает и опечатки, и переименования.
    """
    if unp:
        unp = str(unp).strip()
        for item in profile.organizers:
            if unp in [str(u) for u in item.get("unp", [])]:
                return item
    low = (organizer or "").lower()
    for item in profile.organizers:
        names = item.get("match", "")
        for name in [names] if isinstance(names, str) else names:
            if name and name.lower() in low:
                return item
    return None
