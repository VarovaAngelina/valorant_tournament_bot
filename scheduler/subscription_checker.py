import asyncio

from aiogram import Bot
from loguru import logger

from bot.services.subscription import run_scheduled_subscription_check
from db.session import AsyncSessionLocal


async def subscription_checker_loop(bot: Bot, interval_minutes: int = 20) -> None:
    while True:
        try:
            async with AsyncSessionLocal() as session:
                changed = await run_scheduled_subscription_check(bot, session)
                if changed:
                    logger.info(f"Subscription check updated {changed} registrations")
        except Exception as exc:
            logger.exception(f"Subscription checker failed: {exc}")
        await asyncio.sleep(interval_minutes * 60)
