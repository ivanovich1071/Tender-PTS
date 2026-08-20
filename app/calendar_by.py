"""Рабочие дни Республики Беларусь и запас до окончания подачи.

Зачем отдельный модуль: требование «не менее двух рабочих дней» — жёсткий
фильтр, лот с меньшим запасом в список не попадает вообще. Ошибка здесь
означает либо пропущенный тендер, либо потраченное впустую время оператора,
поэтому логика вынесена и покрыта тестами.

Праздники и переносы приходят из настроек: в Беларуси они объявляются
постановлением на каждый год.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# Время в ГИАС приходит миллисекундами эпохи и означает момент по Минску.
MINSK = timezone(timedelta(hours=3))


def to_minsk(ms: int | float | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(float(ms) / 1000, tz=MINSK)


def is_working_day(day: date, holidays: set[str], working_weekends: set[str]) -> bool:
    key = day.isoformat()
    if key in working_weekends:
        return True
    if key in holidays:
        return False
    return day.weekday() < 5


def working_days_between(start: date, end: date, holidays, working_weekends) -> int:
    """Сколько целых рабочих дней остаётся от start до end.

    Сам день start не считается: если срок истекает сегодня, запаса ноль.
    """
    if end <= start:
        return 0
    holidays = set(holidays)
    working_weekends = set(working_weekends)
    count = 0
    day = start + timedelta(days=1)
    while day <= end:
        if is_working_day(day, holidays, working_weekends):
            count += 1
        day += timedelta(days=1)
    return count


def days_left(deadline_ms: int | float | None, settings: dict,
              now: datetime | None = None) -> int | None:
    """Запас в рабочих днях до окончания подачи. None — срок не указан."""
    deadline = to_minsk(deadline_ms)
    if deadline is None:
        return None
    now = now or datetime.now(MINSK)
    if deadline <= now:
        return 0
    return working_days_between(
        now.date(), deadline.date(),
        settings.get("holidays", []), settings.get("working_weekends", []),
    )


def is_actual(deadline_ms: int | float | None, settings: dict,
              now: datetime | None = None) -> bool:
    """Годится ли лот для работы: срок известен и запас не меньше порога."""
    left = days_left(deadline_ms, settings, now)
    if left is None:
        return False
    return left >= int(settings.get("min_working_days", 2))
