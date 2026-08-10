"""Движок БД и фабрика сессий."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# SQLite нужен для локального прогона без Docker; в бою — Postgres.
IS_SQLITE = settings.database_url.startswith("sqlite")

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=not IS_SQLITE,
)

session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(AsyncAttrs, DeclarativeBase):
    pass


async def create_all() -> None:
    """Создать схему напрямую, без Alembic.

    Только для локального прогона на SQLite: в проде схему ведёт Alembic,
    иначе миграции и фактическая структура разъедутся.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
