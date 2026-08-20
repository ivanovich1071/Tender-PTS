"""Проверки фильтра актуальности.

Требование «не менее двух рабочих дней» — жёсткий фильтр: лот с меньшим
запасом не попадает в список вообще. Ошибка стоит либо пропущенного тендера,
либо потраченного впустую времени оператора, поэтому границы проверяются явно.
"""
from datetime import datetime

from app.calendar_by import MINSK, days_left, is_actual, working_days_between

CFG = {"min_working_days": 2, "holidays": ["2026-08-24"], "working_weekends": []}


def ms(y, m, d, hh=18, mm=0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=MINSK).timestamp() * 1000


def at(y, m, d, hh=10) -> datetime:
    return datetime(y, m, d, hh, 0, tzinfo=MINSK)


def test_weekend_not_counted():
    # чт 20.08.2026 → пн 24.08: пятница рабочая, суббота и воскресенье нет
    assert working_days_between(at(2026, 8, 20).date(), at(2026, 8, 24).date(),
                                [], []) == 2


def test_holiday_not_counted():
    # тот же отрезок, но понедельник объявлен праздником
    assert working_days_between(at(2026, 8, 20).date(), at(2026, 8, 24).date(),
                                ["2026-08-24"], []) == 1


def test_working_saturday_counted():
    assert working_days_between(at(2026, 8, 20).date(), at(2026, 8, 22).date(),
                                [], ["2026-08-22"]) == 2


def test_deadline_tomorrow_is_rejected():
    """Граничный случай: срок «завтра» даёт запас в один день и не проходит."""
    now = at(2026, 8, 20)              # четверг
    assert days_left(ms(2026, 8, 21), CFG, now) == 1
    assert is_actual(ms(2026, 8, 21), CFG, now) is False


def test_deadline_in_two_working_days_passes():
    now = at(2026, 8, 20)
    assert days_left(ms(2026, 8, 25), CFG, now) == 2   # пн 24-е праздник
    assert is_actual(ms(2026, 8, 25), CFG, now) is True


def test_expired_and_missing_rejected():
    now = at(2026, 8, 20)
    assert days_left(ms(2026, 8, 19), CFG, now) == 0
    assert is_actual(ms(2026, 8, 19), CFG, now) is False
    assert days_left(None, CFG, now) is None
    assert is_actual(None, CFG, now) is False
