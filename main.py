# main.py
import asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from loguru import logger

from bot.handlers.user import user_router
from bot.handlers.registration import registration_router
from bot.handlers.subscription import subscription_router
from bot.utils.set_commands import set_bot_commands
from config import settings
from db.bootstrap import ensure_project_settings_table
from db.session import AsyncSessionLocal, engine
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.role import RoleMiddleware
from bot.handlers.admin import admin_router
from bot.handlers.stages import stages_router
from bot.handlers.finalists import finalists_router
from scheduler.subscription_checker import subscription_checker_loop


def create_bot() -> Bot:
    if settings.TELEGRAM_PROXY:
        session = AiohttpSession(proxy=settings.TELEGRAM_PROXY)
        logger.info("Telegram Bot API: используется прокси")
        return Bot(token=settings.BOT_TOKEN, session=session)
    return Bot(token=settings.BOT_TOKEN)


async def main():
    # Явно инициализируем MemoryStorage для FSM
    storage = MemoryStorage()
    bot = create_bot()
    dp = Dispatcher(storage=storage)

    logger.info("Настройка базы данных...")
    async with AsyncSessionLocal() as session:
        await ensure_project_settings_table(session)
    # Открываем асинхронную сессию БД на каждый апдейт
    dp.update.outer_middleware(DbSessionMiddleware(AsyncSessionLocal))

    # Регистрируем мидлварь ролей как OUTER
    dp.update.outer_middleware(RoleMiddleware())

    logger.info("Бот запускается...")
    
    try:
        logger.info("Установка команд меню...")
        try:
            await set_bot_commands(bot)
        except TelegramNetworkError as exc:
            logger.warning(f"Не удалось установить команды меню (бот продолжит работу): {exc}")
        
        # Подключаем роутеры
        dp.include_routers(
            admin_router,
            stages_router,
            finalists_router,
            registration_router,
            subscription_router,
            user_router,
        )

        checker_task = asyncio.create_task(subscription_checker_loop(bot))

        # Запуск long polling с принудительным сбросом зависших вебхуков
        await bot.delete_webhook(drop_pending_updates=True)
        try:
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
            )
        finally:
            checker_task.cancel()
            try:
                await checker_task
            except asyncio.CancelledError:
                pass
    finally:
        logger.info("Закрытие соединений с базой данных...")
        await engine.dispose()
        logger.info("Бот успешно остановлен.")

if __name__ == "__main__":
    logger.add("logs/bot.log", rotation="10 MB", level="INFO", encoding="utf-8")
    asyncio.run(main())