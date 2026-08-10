"""Типы колонок, одинаково работающие на Postgres и SQLite.

SQLite не хранит часовой пояс: записали aware-datetime — прочитали naive,
и сравнение с `utcnow()` падает с TypeError. Поэтому все временные колонки
проходят через UtcDateTime, который нормализует значение в обе стороны.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB


class UtcDateTime(TypeDecorator):
    """DateTime, который всегда отдаёт UTC-aware значение."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(
        self, value: datetime | None, dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite и server_default CURRENT_TIMESTAMP отдают naive —
            # по смыслу это UTC.
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


# На Postgres — JSONB (индексируемый), на SQLite — обычный JSON.
# Внутрь этого поля мы не ищем, поэтому разница некритична.
JsonDict = JSON().with_variant(JSONB(), "postgresql")
