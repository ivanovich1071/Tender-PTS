"""Сбор устойчив к отказу отдельной площадки.

icetrade закрыт для адресов вне Беларуси и без прокси отвечает 403. Это не
должно ронять прогон: ГИАС в это же время работает и обязан собраться до конца,
а причина отказа — попасть в статистику, а не потеряться.
"""
from app import collector
from app.sources.base import SourceError


class Broken:
    name = "broken"
    title = "Сломанная площадка"
    calls = 3

    def harvest(self, profile, cfg, progress=None, cancelled=None):
        raise SourceError("403: адрес не из Беларуси")
        yield  # pragma: no cover — делает функцию генератором


class Working:
    name = "working"
    title = "Рабочая площадка"
    calls = 7

    def harvest(self, profile, cfg, progress=None, cancelled=None):
        purchase = {
            "id": "test-1", "source": "working", "number": "1", "title": "Аукцион",
            "state": "Подача предложений", "tender_form": "Электронный аукцион",
            "organizer": "ОАО «Тест»", "unp": "1", "location": "Минск",
            "sum_lot": 100.0, "created_ms": 1, "updated_ms": 1,
            "deadline_ms": 2, "auction_url": "", "page_url": "", "days_left": 5,
        }
        lots = [{
            "id": "lot-1", "purchase_id": "test-1", "lot_number": 1,
            "title": 'Объемная штамповка из стали "Шестерня" №56',
            "okpb": "25.50.12.300", "volume": 1200.0, "unit": "Штука",
            "price": 86124.0, "delivery": "", "state": "Подача предложений",
        }]
        yield purchase, lots, []


def test_broken_source_does_not_stop_the_run(monkeypatch, tmp_path):
    monkeypatch.setattr(collector.store, "DB", tmp_path / "t.db")
    # Второй уровень отбора здесь ни при чём, а ключ у запускающего может быть
    # настоящим — в сеть из теста не ходим.
    monkeypatch.setattr(collector.judge, "configured", lambda cfg: False)
    monkeypatch.setattr(collector, "build_sources", lambda cfg: [Broken(), Working()])

    stats = collector.collect()

    assert stats["saved_purchases"] == 1
    assert stats["saved_lots"] == 1
    assert stats["sources"]["working"]["saved"] == 1
    assert stats["sources"]["broken"]["saved"] == 0
    assert any("403" in e for e in stats["errors"])
    assert stats["calls"] == 10          # 3 сломанной + 7 рабочей


def test_dismissed_lot_does_not_come_back(monkeypatch, tmp_path):
    """Выброшенное оператором не возвращается следующим прогоном.

    Иначе ручная чистка списка обнулялась бы каждым сбором, и смысла в ней
    не было бы никакого.
    """
    monkeypatch.setattr(collector.store, "DB", tmp_path / "t.db")
    monkeypatch.setattr(collector, "build_sources", lambda cfg: [Working()])
    monkeypatch.setattr(collector.judge, "configured", lambda cfg: False)

    assert collector.collect()["saved_lots"] == 1

    conn = collector.store.connect()
    key = collector.store.lot_key(
        "ОАО «Тест»", 'Объемная штамповка из стали "Шестерня" №56', "25.50.12.300")
    collector.store.dismiss(conn, key, "ОАО «Тест»", "Шестерня №56")
    conn.commit()
    conn.close()

    stats = collector.collect()
    assert stats["saved_lots"] == 0
    assert stats["dismissed"] == 1
