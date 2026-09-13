"""Конфигурация из переменных окружения.

Всё, что можно захотеть поменять без правки кода, живёт здесь.
Секретов в файле нет — только имена переменных, значения приходят из .env.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень проекта — папка, где лежит .env и куда кладётся SQLite-файл.
# Считаем от расположения этого модуля, а не от текущей директории:
# иначе запуск из подпапки не нашёл бы .env и завёл вторую БД.
ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Plan:
    """Тариф. Цена в минимальных единицах валюты (для XTR — целые звёзды)."""

    code: str
    title: str
    months: int
    price: int


# Цены правятся здесь и больше нигде.
PLANS: tuple[Plan, ...] = (
    Plan(code="m1", title="1 месяц", months=1, price=100),
    Plan(code="m3", title="3 месяца", months=3, price=270),
    Plan(code="m6", title="6 месяцев", months=6, price=500),
    Plan(code="m12", title="12 месяцев", months=12, price=900),
)

PLANS_BY_CODE: dict[str, Plan] = {p.code: p for p in PLANS}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str
    admin_ids: str = ""

    # Прокси до api.telegram.org — в РФ он часто недоступен напрямую.
    # http://host:port, http://user:pass@host:port или socks5://host:port
    # (для socks5 нужен пакет aiohttp-socks).
    telegram_proxy: str = ""

    # По умолчанию — файл в корне проекта: локальный запуск не требует
    # ни Docker, ни Postgres. В compose переменная переопределяется.
    database_url: str = f"sqlite+aiosqlite:///{ROOT / 'vpn_bot.db'}"

    # Создавать схему при старте вместо Alembic. Включается автоматически
    # для SQLite — на Postgres схему ведут миграции.
    auto_create_schema: bool | None = None

    sub_base_url: str = "http://127.0.0.1:8080"
    sub_port: int = 8080
    sub_title: str = "BLAZER VPN"
    sub_update_interval_hours: int = 12
    support_url: str = ""
    trust_proxy_headers: bool = False

    trial_days: int = 2
    device_limit: int = 6
    panel_set_expiry: bool = False

    log_level: str = "INFO"

    # Валюта инвойсов. XTR — Telegram Stars, единственный провайдер,
    # не требующий внешней регистрации.
    currency: str = "XTR"

    @property
    def admins(self) -> set[int]:
        return {
            int(chunk)
            for chunk in self.admin_ids.replace(" ", "").split(",")
            if chunk
        }

    @property
    def should_create_schema(self) -> bool:
        if self.auto_create_schema is not None:
            return self.auto_create_schema
        return self.database_url.startswith("sqlite")

    def sub_url(self, token: str) -> str:
        return f"{self.sub_base_url.rstrip('/')}/s/{token}"


settings = Settings()  # type: ignore[call-arg]
