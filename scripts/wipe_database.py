"""
Полная очистка всех таблиц бота (удаление тестовых и рабочих данных).

  python scripts/wipe_database.py

Локально (Docker MySQL на 3307):
  set DB_HOST=127.0.0.1
  set DB_PORT=3307
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_PORT", "3307")

from sqlalchemy import text

from config import settings
from db.session import AsyncSessionLocal

TABLES = (
    "notifications_log",
    "replacement_log",
    "mvp_awards",
    "finalists",
    "stage_results",
    "stage_team_members",
    "stage_teams",
    "stages",
    "group_members",
    "tournament_groups",
    "subscription_events",
    "registrations",
    "tournament_settings",
    "project_settings",
    "tournaments",
    "users",
    "admins",
)


async def wipe(*, force: bool) -> None:
    if not force:
        print("Добавьте --force для подтверждения полной очистки БД.")
        return

    print(
        f"Очистка БД: {settings.DB_USER}@{os.environ.get('DB_HOST', settings.DB_HOST)}:"
        f"{os.environ.get('DB_PORT', settings.DB_PORT)}/{settings.DB_NAME}"
    )

    async with AsyncSessionLocal() as session:
        await session.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in TABLES:
            await session.execute(text(f"TRUNCATE TABLE `{table}`"))
            print(f"  truncated {table}")
        await session.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        await session.commit()

    print("Готово: все таблицы очищены.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Полная очистка БД valorant_bot")
    parser.add_argument("--force", action="store_true", help="Подтвердить очистку")
    args = parser.parse_args()
    asyncio.run(wipe(force=args.force))


if __name__ == "__main__":
    main()
