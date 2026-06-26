from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

DEV_MODE_TOGGLE_CALLBACK = "dev_toggle_mode"


def remove_reply_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


async def clear_reply_keyboard(message: Message) -> None:
    """Снимает reply-клавиатуру без лишних сообщений в чате."""
    try:
        tmp = await message.answer("\u2060", reply_markup=remove_reply_keyboard())
        await tmp.delete()
    except Exception:
        pass


def append_developer_mode_toggle(
    buttons: list[list[InlineKeyboardButton]],
    role: str,
    *,
    admin_mode: bool,
) -> None:
    if role != "developer":
        return
    label = "🔄 Режим: ЮЗЕР" if admin_mode else "🔄 Режим: АДМИН"
    buttons.append([
        InlineKeyboardButton(text=label, callback_data=DEV_MODE_TOGGLE_CALLBACK),
    ])


def get_user_inline_menu(
    role: str,
    *,
    is_participant: bool = False,
    tournament_active: bool = False,
    can_edit_profile: bool = False,
    can_register: bool = False,
    admin_mode: bool = False,
    rules_url: str | None = None,
) -> InlineKeyboardMarkup:
    if admin_mode and role == "developer":
        return InlineKeyboardMarkup(inline_keyboard=[])

    buttons: list[list[InlineKeyboardButton]] = []

    if rules_url:
        rules_button = InlineKeyboardButton(text="📜 Регламент", url=rules_url)
    else:
        rules_button = InlineKeyboardButton(text="📜 Регламент", callback_data="user_menu_rules")
    buttons.append([
        rules_button,
        InlineKeyboardButton(text="📜 История", callback_data="user_menu_history"),
        InlineKeyboardButton(text="🏅 Моя история", callback_data="user_menu_my_history"),
    ])

    if can_register:
        buttons.append([
            InlineKeyboardButton(text="📝 Регистрация", callback_data="user_menu_register"),
        ])

    if can_edit_profile:
        buttons.append([
            InlineKeyboardButton(text="✏️ Профиль", callback_data="user_menu_edit_profile"),
            InlineKeyboardButton(text="❌ Отозвать заявку", callback_data="user_menu_withdraw"),
        ])

    if tournament_active:
        buttons.append([
            InlineKeyboardButton(text="📊 Статистика", callback_data="user_menu_stats"),
            InlineKeyboardButton(text="👥 Группы", callback_data="user_menu_groups"),
        ])

    append_developer_mode_toggle(buttons, role, admin_mode=False)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_user_main_menu(*args, **kwargs) -> InlineKeyboardMarkup:
    if "is_registered" in kwargs:
        kwargs["is_participant"] = kwargs.pop("is_registered")
    return get_user_inline_menu(*args, **kwargs)
