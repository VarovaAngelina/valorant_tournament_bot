# bot/filters/role.py
from aiogram.filters import Filter
from aiogram.types import Message, CallbackQuery
from typing import Any

class RoleFilter(Filter):
    def __init__(self, *allowed_roles: str):
        self.allowed_roles = allowed_roles

    async def __call__(self, event: Message | CallbackQuery, **kwargs: Any) -> bool:
        # Безопасно достаем роль, которую Middleware положил в data
        role = kwargs.get("role")
        
        # Если это создатель/разработчик бота — ему можно всё
        if role == "developer":
            return True
            
        # Для остальных проверяем, входит ли роль в список разрешенных
        return role in self.allowed_roles