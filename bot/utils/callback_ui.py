"""Единый способ обновлять inline-меню без лишних сообщений в чате."""

from aiogram import types


async def reply_or_edit(
    callback: types.CallbackQuery,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
) -> None:
    """
    Редактирует сообщение с inline-кнопками.
    Новое сообщение отправляется только для fake-callback (id == \"0\")
    или если Telegram не позволяет отредактировать текст.
    """
    if callback.id == "0":
        await callback.message.answer(text, reply_markup=reply_markup)
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup)


async def edit_or_notify(
    callback: types.CallbackQuery,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
) -> None:
    """Только редактирование; без fallback на новое сообщение (для навигации по меню)."""
    if callback.id == "0":
        await callback.message.answer(text, reply_markup=reply_markup)
        return
    await callback.message.edit_text(text, reply_markup=reply_markup)
