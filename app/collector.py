"""Сбор: источники → отбор по профилю → база.

Источники независимы и складываются в общий список. Падение одного не должно
ронять прогон: icetrade закрыт для адресов вне Беларуси и без прокси ответит
403, но ГИАС при этом работает и должен собраться до конца. Поэтому ошибка
источника попадает в статистику, а не наружу.

Отбор и фильтр актуальности одинаковы для всех площадок и живут здесь;
знание о том, какие у площадки состояния и как её листать, — внутри источника.
"""
from __future__ import annotations

from app import matching, profile as profile_mod, settings, store
from app.sources.base import SourceError
from app.sources.gias import GiasSource
from app.sources.icetrade import Icetrade


def build_sources(cfg: dict) -> list:
    """Включённые источники в порядке приоритета."""
    enabled = cfg.get("sources") or {}
    sources = []
    if enabled.get("gias", True):
        sources.append(GiasSource())
    if enabled.get("icetrade"):
        sources.append(Icetrade(cfg))
    return sources


def collect(progress=None, cancelled=None) -> dict:
    """Один прогон сбора по всем включённым источникам."""
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    cfg = settings.read()
    prof = profile_mod.load()

    stats = {
        "sources": {}, "saved_purchases": 0, "saved_lots": 0,
        "no_match": 0, "calls": 0, "errors": [], "warnings": [],
    }

    conn = store.connect()
    try:
        for source in build_sources(cfg):
            found = saved = 0
            try:
                for purchase, lots, files in source.harvest(
                        prof, cfg, progress=say, cancelled=cancelled):
                    found += 1
                    kept = _keep(conn, prof, purchase, lots, files)
                    if kept:
                        saved += 1
                        stats["saved_purchases"] += 1
                        stats["saved_lots"] += kept
                    else:
                        stats["no_match"] += 1
            except SourceError as e:
                # Ожидаемая беда площадки: нет доступа, не настроена, ответила не тем.
                stats["errors"].append(f"{source.title}: {e}")
            except Exception as e:
                stats["errors"].append(
                    f"{source.title}: непредвиденная ошибка {type(e).__name__}: {e}")
            stats["sources"][source.name] = {
                "title": source.title, "actual": found, "saved": saved}
            stats["calls"] += getattr(source, "calls", 0)
            stats["warnings"] += getattr(source, "warnings", [])
            if cancelled and cancelled():
                break
        conn.commit()
        stats["duplicates"] = store.mark_duplicates(conn)
    finally:
        conn.close()

    say("готово")
    return stats


def _keep(conn, prof, purchase: dict, lots: list[dict], files: list[dict]) -> int:
    """Отобрать лоты закупки по профилю и записать. Возвращает число оставленных."""
    filenames = [f["name"] or "" for f in files]
    watched = matching.match_by_organizer(
        prof, purchase.get("organizer"), purchase.get("unp"))

    kept = []
    for lot in lots:
        m = matching.match_lot(
            prof, lot["title"], purchase.get("title", ""), lot["okpb"], filenames)
        if not m.matched and watched:
            # Заказчик под наблюдением: его лоты берём и без совпадения по словам —
            # закупки называются «Аукцион», а деталь видна только внутри лота.
            kind = matching.classify_kind(
                prof, f"{lot['title']} {purchase.get('title', '')}")
            m = matching.Match(True, kind, "Заказчик под наблюдением", [],
                               f"заказчик в списке наблюдения: {watched['why']}")
        if not m.matched or m.kind == "stop":
            continue
        kept.append({**lot, **m.as_row()})

    if not kept:
        return 0
    store.save_purchase(conn, purchase, kept, files)
    return len(kept)
