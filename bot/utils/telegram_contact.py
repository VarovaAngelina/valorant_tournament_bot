from aiogram import Bot


async def resolve_telegram_contact(bot: Bot, text: str) -> tuple[int, str | None, str]:
    raw = text.strip()
    if not raw:
        raise ValueError("Укажите Telegram ID или @username.")

    if raw.isdigit():
        tg_id = int(raw)
        try:
            tg_user = await bot.get_chat(tg_id)
            username = tg_user.username
            contact = f"@{username}" if username else tg_user.full_name
            return tg_id, username, contact
        except Exception:
            return tg_id, None, f"ID {tg_id}"

    username = raw.lstrip("@")
    if not username or " " in username:
        raise ValueError("Укажите числовой Telegram ID или @username без пробелов.")

    try:
        tg_user = await bot.get_chat(f"@{username}")
    except Exception as exc:
        raise ValueError(
            "Не удалось найти пользователя в Telegram. "
            "Проверьте @username или укажите числовой ID."
        ) from exc

    contact = f"@{tg_user.username}" if tg_user.username else tg_user.full_name
    return tg_user.id, tg_user.username, contact
