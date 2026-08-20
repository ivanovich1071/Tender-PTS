"""Сбор: площадка → отбор по профилю → база.

Порядок шагов выбран ради экономии запросов. Выдача поиска уже содержит
состояние процедуры и срок подачи, поэтому неактуальное отсеивается до
загрузки карточек: закрытых процедур в выдаче кратно больше, чем открытых,
и карточка каждой стоит отдельного запроса.

Требование «не менее двух рабочих дней» — жёсткое: такие лоты не попадают
в базу вообще, а не помечаются флажком. Оператор не должен видеть то, на что
всё равно не успеет.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import calendar_by, matching, profile as profile_mod, settings, store
from app.sources.gias import Gias, is_active_state, parse_card


def collect(progress=None, cancelled=None) -> dict:
    """Один прогон сбора. Возвращает статистику."""
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    def stop() -> bool:
        return bool(cancelled and cancelled())

    cfg = settings.read()
    prof = profile_mod.load()
    src = Gias(proxy=cfg.get("proxy", ""), pause=float(cfg.get("request_pause", 0.4)))

    window = int(cfg.get("window_days", 30))
    cutoff_ms = (datetime.now(timezone.utc) - timedelta(days=window)).timestamp() * 1000

    queries = [(text, group) for text, group in prof.queries()]
    queries += [(o["query"], "") for o in prof.organizers]

    stats = {
        "queries": len(queries), "seen": 0, "active": 0, "cards": 0,
        "saved_purchases": 0, "saved_lots": 0, "skipped_deadline": 0,
        "no_match": 0, "calls": 0, "errors": [],
    }

    # --- 1. Поиск и отсев по сроку до загрузки карточек ---
    candidates: dict[str, dict] = {}
    for i, (query, _group) in enumerate(queries, 1):
        if stop():
            say("отменено")
            return stats
        say(f"поиск {i}/{len(queries)}: {query}")
        try:
            items = src.find_items(query, cutoff_ms)
        except Exception as e:
            stats["errors"].append(f"поиск «{query}»: {type(e).__name__}: {e}")
            continue
        stats["seen"] += len(items)
        for item in items:
            if not is_active_state(item.get("stateName")):
                continue
            if not calendar_by.is_actual(item.get("requestDate"), cfg):
                stats["skipped_deadline"] += 1
                continue
            candidates.setdefault(item["purchaseGiasId"], item)
    stats["active"] = len(candidates)

    # --- 2. Карточки, отбор лотов, запись ---
    conn = store.connect()
    try:
        for i, uuid in enumerate(candidates, 1):
            if stop():
                say("отменено")
                break
            if i % 10 == 0 or i == 1:
                say(f"карточки {i}/{len(candidates)}")
            try:
                card = src.card(uuid)
            except Exception as e:
                stats["errors"].append(f"карточка {uuid}: {type(e).__name__}: {e}")
                continue
            if not card:
                continue
            stats["cards"] += 1

            purchase, lots, files = parse_card(card, uuid)
            purchase["days_left"] = calendar_by.days_left(purchase["deadline_ms"], cfg)
            filenames = [f["name"] or "" for f in files]
            watched = matching.match_by_organizer(prof, purchase["organizer"])

            kept = []
            for lot in lots:
                m = matching.match_lot(
                    prof, lot["title"], purchase["title"], lot["okpb"], filenames)
                if not m.matched and watched:
                    # Заказчик под наблюдением: его лоты берём и без совпадения
                    # по словам — их номенклатура словами почти не ищется.
                    kind = matching.classify_kind(
                        prof, f"{lot['title']} {purchase['title']}")
                    m = matching.Match(True, kind, "Заказчик под наблюдением", [],
                                       f"заказчик в списке наблюдения: {watched['why']}")
                if not m.matched or m.kind == "stop":
                    continue
                kept.append({**lot, **m.as_row()})

            if not kept:
                stats["no_match"] += 1
                continue
            store.save_purchase(conn, purchase, kept, files)
            stats["saved_purchases"] += 1
            stats["saved_lots"] += len(kept)
        conn.commit()
    finally:
        conn.close()

    stats["calls"] = src.calls
    say("готово")
    return stats
