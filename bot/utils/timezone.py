"""Московское время (MSK, UTC+3) для записи в БД и отображения пользователю."""

from datetime import datetime
from zoneinfo import ZoneInfo

# Часовой пояс турниров — всегда Москва.
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def now_moscow() -> datetime:
    """Текущие дата и время в Москве (naive datetime для колонок DateTime)."""
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)


def format_moscow(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Форматирует сохранённую метку времени для сообщений бота."""
    if not dt:
        return "—"
    return dt.strftime(fmt)


def format_moscow_date(dt: datetime | None) -> str:
    """Короткая дата без времени."""
    return format_moscow(dt, "%d.%m.%Y")
