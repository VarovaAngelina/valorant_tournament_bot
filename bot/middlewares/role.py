# bot/middlewares/role.py
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, Update
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from config import settings
from db.models import Admin, AdminStatus


async def resolve_user_role(db_session: AsyncSession | None, telegram_id: int) -> str:
    if telegram_id == settings.DEVELOPER_TG_ID:
        return "developer"

    if db_session is None:
        return "user"

    admin = (
        await db_session.execute(select(Admin).where(Admin.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if admin and admin.admin_status == AdminStatus.ACTIVE:
        return "admin"

    return "user"


class RoleMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        current_event = event.event

        user = None
        if isinstance(current_event, Message):
            user = current_event.from_user
        elif isinstance(current_event, CallbackQuery):
            user = current_event.from_user

        if not user:
            logger.warning("[RoleMiddleware] Не удалось определить пользователя из event")
            return await handler(event, data)

        user_id = user.id
        db_session: AsyncSession | None = data.get("db_session")
        role = await resolve_user_role(db_session, user_id)
        data["role"] = role

        logger.info(f"[RoleMiddleware] Пользователю {user_id} присвоена роль: {role}")
        return await handler(event, data)
