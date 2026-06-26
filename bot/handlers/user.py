# bot/handlers/user.py
# Меню участника: регистрация, профиль, группы, статистика, личная история.
from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from bot.keyboards.menu import DEV_MODE_TOGGLE_CALLBACK, clear_reply_keyboard
from bot.keyboards.ranks import get_ranks_keyboard, get_tiers_keyboard
from bot.services.scoring import format_personal_stats_text
from bot.utils.timezone import now_moscow
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from db.models import Tournament, TournamentStatus, Registration, RegistrationStatus, User
from aiogram.fsm.state import State, StatesGroup

user_router = Router()

class ProfileEditStates(StatesGroup):
    waiting_for_field_choice = State()
    waiting_for_new_nick = State()
    waiting_for_new_rank = State()
    waiting_for_new_rank_tier = State()


def _is_valid_riot_id(riot_id: str) -> bool:
    return "#" in riot_id and len(riot_id.strip()) >= 5


def _profile_field_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="🎮 Riot ID", callback_data="profile_edit_nick")],
            [types.InlineKeyboardButton(text="🏅 Ранг", callback_data="profile_edit_rank")],
        ]
    )


async def _finish_profile_edit(
    message: types.Message,
    state: FSMContext,
    role: str,
    db_session: AsyncSession,
    success_text: str,
    *,
    telegram_id: int | None = None,
) -> None:
    state_data = await state.get_data()
    admin_mode = state_data.get("admin_mode", False)
    await state.clear()
    if admin_mode:
        await state.update_data(admin_mode=admin_mode)

    user_id = telegram_id or message.from_user.id
    markup = await build_user_inline_menu(db_session, user_id, role, state)
    await _edit_or_answer(message, success_text, reply_markup=markup)


async def _load_editable_registration(
    db_session: AsyncSession,
    reg_id: int,
) -> Registration | None:
    return (
        await db_session.execute(
            select(Registration).where(
                Registration.id == reg_id,
                Registration.status.in_(
                    (
                        RegistrationStatus.REGISTERED,
                        RegistrationStatus.SELECTED_MAIN,
                        RegistrationStatus.SELECTED_RESERVE,
                    )
                ),
            )
        )
    ).scalar_one_or_none()


from bot.services.user_menu import build_user_inline_menu, get_user_menu_context


async def _edit_or_answer(
    message: types.Message,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await message.answer(text, reply_markup=reply_markup)


async def _get_panel_message_id(state: FSMContext) -> int | None:
    state_data = await state.get_data()
    return state_data.get("panel_message_id") or state_data.get("menu_message_id")


async def _set_panel_message_id(state: FSMContext, message_id: int) -> None:
    await state.update_data(panel_message_id=message_id, menu_message_id=message_id)


async def _edit_or_send_panel(
    message: types.Message,
    state: FSMContext,
    text: str,
    reply_markup: types.InlineKeyboardMarkup,
) -> None:
    panel_message_id = await _get_panel_message_id(state)
    if panel_message_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=panel_message_id,
                text=text,
                reply_markup=reply_markup,
            )
            await _set_panel_message_id(state, panel_message_id)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                await _set_panel_message_id(state, panel_message_id)
                return
        except Exception:
            pass

    sent = await message.answer(text, reply_markup=reply_markup)
    await _set_panel_message_id(state, sent.message_id)


async def _switch_developer_mode(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
    role: str,
) -> None:
    state_data = await state.get_data()
    new_mode = not state_data.get("admin_mode", False)
    await state.update_data(admin_mode=new_mode)

    if new_mode:
        from bot.handlers.admin import build_admin_home_content

        content = await build_admin_home_content(db_session, role, message.from_user.id)
        if not content:
            await message.answer("❌ Не удалось открыть панель администратора.")
            return
        admin_text, markup = content
        panel_text = f"Интерфейс переключен в режим администратора.\n\n{admin_text}"
        await _edit_or_send_panel(message, state, panel_text, markup)
        return

    markup = await _build_user_home_markup(db_session, message.from_user.id, role, state)
    panel_text = (
        "Интерфейс переключен в режим пользователя.\n\n"
        "👋 Добро пожаловать на турнир по Valorant!\nВыберите действие:"
    )
    await _edit_or_send_panel(message, state, panel_text, markup)


async def _send_user_home(
    target: types.Message,
    db_session: AsyncSession,
    role: str,
    state: FSMContext,
    *,
    text: str | None = None,
) -> None:
    home_text = text or "👋 Добро пожаловать на турнир по Valorant!\nВыберите действие:"
    markup = await build_user_inline_menu(db_session, target.from_user.id, role, state)
    sent = await target.answer(home_text, reply_markup=markup)
    await _set_panel_message_id(state, sent.message_id)


@user_router.message(CommandStart())
async def cmd_start(message: types.Message, role: str, state: FSMContext, db_session: AsyncSession):
    if role == "admin":
        await clear_reply_keyboard(message)
        from bot.handlers.admin import send_admin_home
        await send_admin_home(message, db_session, role)
        return

    await clear_reply_keyboard(message)

    state_data = await state.get_data()
    admin_mode = state_data.get("admin_mode", False)

    if admin_mode:
        from bot.handlers.admin import build_admin_home_content

        content = await build_admin_home_content(db_session, role, message.from_user.id)
        if content:
            admin_text, markup = content
            home_msg = await message.answer(admin_text, reply_markup=markup)
            await _set_panel_message_id(state, home_msg.message_id)
            return

    home_msg = await message.answer(
        "👋 Добро пожаловать на турнир по Valorant!\nВыберите действие:",
        reply_markup=await build_user_inline_menu(db_session, message.from_user.id, role, state),
    )
    await _set_panel_message_id(state, home_msg.message_id)


@user_router.callback_query(F.data == DEV_MODE_TOGGLE_CALLBACK)
async def toggle_developer_mode_callback(
    callback: types.CallbackQuery,
    role: str,
    state: FSMContext,
    db_session: AsyncSession,
):
    if role != "developer":
        await callback.answer("Недоступно", show_alert=True)
        return
    await callback.answer()
    await _switch_developer_mode(callback.message, state, db_session, role)


@user_router.message(F.text.startswith("🔄 Режим:"))
async def legacy_mode_reply_cleanup(
    message: types.Message,
    role: str,
    state: FSMContext,
    db_session: AsyncSession,
):
    """Удаляет сообщения от старой reply-клавиатуры и переключает режим."""
    if role != "developer":
        return
    try:
        await message.delete()
    except Exception:
        pass
    await clear_reply_keyboard(message)
    await _switch_developer_mode(message, state, db_session, role)


@user_router.callback_query(F.data == "user_menu_home")
async def user_menu_home(callback: types.CallbackQuery, role: str, state: FSMContext, db_session: AsyncSession):
    await callback.answer()
    if role == "admin":
        from bot.handlers.admin import send_admin_home
        await send_admin_home(callback.message, db_session, role)
        return
    await callback.message.edit_text(
        "👋 Добро пожаловать на турнир по Valorant!\nВыберите действие:",
        reply_markup=(await _build_user_home_markup(db_session, callback.from_user.id, role, state)),
    )
    await _set_panel_message_id(state, callback.message.message_id)


async def _build_user_home_markup(
    db_session: AsyncSession,
    telegram_id: int,
    role: str,
    state: FSMContext,
) -> types.InlineKeyboardMarkup:
    return await build_user_inline_menu(db_session, telegram_id, role, state)


@user_router.callback_query(F.data == "user_menu_rules")
async def user_menu_rules(callback: types.CallbackQuery):
    await callback.answer("📜 Регламент ещё не опубликован администратором.", show_alert=True)


@user_router.callback_query(F.data == "user_menu_history")
async def user_menu_history(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.tournament_history import format_archive_list_text

    await callback.answer()
    text = await format_archive_list_text(db_session, admin_view=False)
    buttons = [[types.InlineKeyboardButton(text="⬅️ Главное меню", callback_data="user_menu_home")]]
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@user_router.callback_query(F.data == "user_menu_my_history")
async def user_menu_my_history(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.personal_history import format_user_personal_history

    await callback.answer()
    text = await format_user_personal_history(db_session, callback.from_user.id)
    buttons = [[types.InlineKeyboardButton(text="⬅️ Главное меню", callback_data="user_menu_home")]]
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@user_router.callback_query(F.data == "user_menu_groups")
async def user_menu_groups(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.grouping import get_tournament_groups
    from bot.services.tournament_helpers import USER_LIVE_TOURNAMENT_STATUSES, get_latest_tournament

    await callback.answer()
    active_tour = await get_latest_tournament(db_session, USER_LIVE_TOURNAMENT_STATUSES)
    if not active_tour:
        await callback.message.edit_text("📋 Группы доступны только во время активного турнира.")
        return

    groups = await get_tournament_groups(db_session, active_tour.id)
    if not groups:
        await callback.message.edit_text("📋 Группы для этого турнира ещё не сформированы.")
        return

    buttons = [
        [types.InlineKeyboardButton(
            text=f"Группа {group.group_number}",
            callback_data=f"user_group_{active_tour.id}_{group.id}",
        )]
        for group in groups
    ]
    buttons.append([types.InlineKeyboardButton(text="⬅️ Главное меню", callback_data="user_menu_home")])
    await callback.message.edit_text(
        "👥 Выберите группу для просмотра состава и раундов:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@user_router.callback_query(F.data == "user_menu_stats")
async def user_menu_stats(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.tournament_helpers import USER_LIVE_TOURNAMENT_STATUSES, get_latest_tournament

    await callback.answer()
    active_tour = await get_latest_tournament(db_session, USER_LIVE_TOURNAMENT_STATUSES)
    if not active_tour:
        await callback.message.edit_text("📊 Статистика доступна только во время активного турнира.")
        return

    text = await format_personal_stats_text(db_session, active_tour.id, callback.from_user.id)
    buttons = [[types.InlineKeyboardButton(text="⬅️ Главное меню", callback_data="user_menu_home")]]
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@user_router.callback_query(F.data == "user_menu_withdraw")
async def user_menu_withdraw(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    query = select(Registration).join(User).where(
        User.telegram_id == callback.from_user.id,
        Registration.status == RegistrationStatus.REGISTERED,
    ).order_by(Registration.id.desc()).limit(1)
    reg = (await db_session.execute(query)).scalar_one_or_none()

    if not reg:
        await callback.message.edit_text("❌ У вас нет активной заявки на турнир.")
        return

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == reg.tournament_id))
    ).scalar_one_or_none()

    if not tour or tour.status not in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
    ):
        await callback.message.edit_text(
            "🚫 Отозвать заявку уже нельзя: отбор участников начался или турнир завершён."
        )
        return

    await callback.message.edit_text(
        f"⚠️ Вы уверены, что хотите отозвать заявку на турнир «{tour.title}»?\n"
        f"Riot ID: {reg.game_nick}",
        reply_markup=_withdraw_confirm_keyboard(),
    )


@user_router.callback_query(F.data == "user_menu_edit_profile")
async def user_menu_edit_profile(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
):
    await callback.answer()
    await _start_edit_profile(callback.from_user.id, callback.message, state, db_session)


async def _start_edit_profile(
    telegram_id: int,
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
) -> None:
    query = select(Registration).join(User).where(
        User.telegram_id == telegram_id,
        Registration.status.in_(
            (
                RegistrationStatus.REGISTERED,
                RegistrationStatus.SELECTED_MAIN,
                RegistrationStatus.SELECTED_RESERVE,
            )
        ),
    ).order_by(Registration.id.desc()).limit(1)

    active_reg = (await db_session.execute(query)).scalar_one_or_none()
    if not active_reg:
        await _edit_or_answer(message, "❌ У вас нет активной заявки на турнир.")
        return

    tour = (
        await db_session.execute(
            select(Tournament).where(Tournament.id == active_reg.tournament_id)
        )
    ).scalar_one_or_none()

    if not tour or tour.status not in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.CONFIRMATION_PENDING,
    ):
        await _edit_or_answer(
            message,
            "🚫 Нельзя изменить профиль: отбор уже начался или турнир завершён.",
        )
        return

    await state.update_data(edit_reg_id=active_reg.id)
    await _edit_or_answer(
        message,
        f"📝 Текущий профиль:\n"
        f"• Riot ID: {active_reg.game_nick}\n"
        f"• Ранг: {active_reg.game_rank}\n\n"
        "Выберите, что хотите изменить:",
        reply_markup=_profile_field_keyboard(),
    )
    await state.set_state(ProfileEditStates.waiting_for_field_choice)


@user_router.callback_query(F.data.startswith("user_group_"))
async def user_view_group(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.grouping import format_single_group_text
    from bot.services.stages import format_group_rounds_text

    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[2])
    group_id = int(parts[3])

    group_text = await format_single_group_text(db_session, tour_id, group_id)
    rounds_text = await format_group_rounds_text(db_session, tour_id, group_id)
    buttons = [[types.InlineKeyboardButton(
        text="⬅️ К списку групп",
        callback_data=f"user_groups_menu_{tour_id}",
    )]]
    buttons.append([types.InlineKeyboardButton(text="⬅️ Главное меню", callback_data="user_menu_home")])
    await callback.message.edit_text(
        f"{group_text}\n\n{rounds_text}",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@user_router.callback_query(F.data.startswith("user_groups_menu_"))
async def user_groups_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.grouping import get_tournament_groups

    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    groups = await get_tournament_groups(db_session, tour_id)
    buttons = [
        [types.InlineKeyboardButton(
            text=f"Группа {group.group_number}",
            callback_data=f"user_group_{tour_id}_{group.id}",
        )]
        for group in groups
    ]
    buttons.append([types.InlineKeyboardButton(text="⬅️ Главное меню", callback_data="user_menu_home")])
    await callback.message.edit_text(
        "👥 Выберите группу для просмотра состава и раундов:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@user_router.callback_query(F.data.startswith("confirm_participation_"))
async def confirm_participation(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    reg_id = int(callback.data.split("_")[-1])
    reg = (
        await db_session.execute(
            select(Registration).join(User).where(
                Registration.id == reg_id,
                User.telegram_id == callback.from_user.id,
            )
        )
    ).scalar_one_or_none()

    if not reg:
        await callback.message.edit_text("❌ Заявка не найдена.")
        return

    if reg.participation_confirmed:
        await callback.message.edit_text("ℹ️ Вы уже подтвердили участие.")
        return

    from datetime import datetime
    from bot.services.grouping import (
        all_main_roster_confirmed,
        notify_admins_all_main_roster_confirmed,
    )

    was_all_confirmed = await all_main_roster_confirmed(db_session, reg.tournament_id)
    reg.participation_confirmed = True
    reg.participation_confirmed_at = now_moscow()
    await db_session.commit()

    if not was_all_confirmed and await all_main_roster_confirmed(db_session, reg.tournament_id):
        await notify_admins_all_main_roster_confirmed(
            callback.bot,
            db_session,
            reg.tournament_id,
        )

    await callback.message.edit_text("✅ Участие подтверждено. Ожидайте формирования групп и матчей.")


async def _load_declinable_registration(
    db_session: AsyncSession,
    reg_id: int,
    telegram_id: int,
) -> Registration | None:
    return (
        await db_session.execute(
            select(Registration).join(User).where(
                Registration.id == reg_id,
                User.telegram_id == telegram_id,
                Registration.status == RegistrationStatus.SELECTED_MAIN,
            )
        )
    ).scalar_one_or_none()


@user_router.callback_query(F.data.startswith("decline_participation_confirm_"))
async def decline_participation_confirm(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    reg_id = int(callback.data.split("_")[-1])
    reg = await _load_declinable_registration(db_session, reg_id, callback.from_user.id)

    if not reg:
        await callback.message.edit_text("❌ Заявка не найдена.")
        return

    if reg.participation_confirmed:
        await callback.message.edit_text("ℹ️ Вы уже подтвердили участие.")
        return

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == reg.tournament_id))
    ).scalar_one_or_none()
    if not tour or tour.status != TournamentStatus.CONFIRMATION_PENDING:
        await callback.message.edit_text("🚫 Отказ от участия сейчас недоступен.")
        return

    from bot.services.grouping import notify_admins_player_declined_participation

    reg.status = RegistrationStatus.NOT_SELECTED
    reg.exclusion_reason = "Отказ от участия"
    reg.excluded_at = now_moscow()

    await db_session.commit()

    await notify_admins_player_declined_participation(
        callback.bot, db_session, reg.tournament_id, reg
    )

    await callback.message.edit_text(
        "❌ Вы отказались от участия в турнире. Спасибо, что сообщили заранее.\n"
        "Замену в основной состав выполнит администратор."
    )


@user_router.callback_query(F.data.startswith("decline_participation_cancel_"))
async def decline_participation_cancel(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.replacements import confirm_participation_keyboard

    await callback.answer()
    reg_id = int(callback.data.split("_")[-1])
    reg = await _load_declinable_registration(db_session, reg_id, callback.from_user.id)

    if not reg:
        await callback.message.edit_text("❌ Заявка не найдена.")
        return

    if reg.participation_confirmed:
        await callback.message.edit_text("ℹ️ Вы уже подтвердили участие.")
        return

    await callback.message.edit_text(
        "📨 Подтвердите готовность участвовать в турнире:",
        reply_markup=confirm_participation_keyboard(reg_id),
    )


@user_router.callback_query(F.data.regexp(r"^decline_participation_\d+$"))
async def decline_participation(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.replacements import decline_participation_confirm_keyboard

    await callback.answer()
    reg_id = int(callback.data.split("_")[-1])
    reg = await _load_declinable_registration(db_session, reg_id, callback.from_user.id)

    if not reg:
        await callback.message.edit_text("❌ Заявка не найдена.")
        return

    if reg.participation_confirmed:
        await callback.message.edit_text("ℹ️ Вы уже подтвердили участие.")
        return

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == reg.tournament_id))
    ).scalar_one_or_none()
    if not tour or tour.status != TournamentStatus.CONFIRMATION_PENDING:
        await callback.message.edit_text("🚫 Отказ от участия сейчас недоступен.")
        return

    await callback.message.edit_text(
        f"⚠️ Вы уверены, что хотите отказаться от участия в турнире «{tour.title}»?\n"
        f"Riot ID: {reg.game_nick}\n\n"
        "После отказа вернуться в основной состав будет нельзя.",
        reply_markup=decline_participation_confirm_keyboard(reg_id),
    )


@user_router.callback_query(F.data.startswith("confirm_finalist_"))
async def confirm_finalist(callback: types.CallbackQuery, db_session: AsyncSession):
    from db.models import Finalist

    await callback.answer()
    finalist_id = int(callback.data.split("_")[-1])
    row = (
        await db_session.execute(
            select(Finalist, Registration)
            .join(Registration, Registration.id == Finalist.registration_id)
            .join(User, User.id == Registration.user_id)
            .where(
                Finalist.id == finalist_id,
                User.telegram_id == callback.from_user.id,
            )
        )
    ).first()

    if not row:
        await callback.message.edit_text("❌ Запись финалиста не найдена.")
        return

    finalist, _registration = row
    if finalist.participation_confirmed:
        await callback.message.edit_text("ℹ️ Вы уже подтвердили участие в финале.")
        return

    from bot.services.final_stage import all_finalists_confirmed, notify_admins_all_finalists_confirmed

    was_all_confirmed = await all_finalists_confirmed(db_session, finalist.tournament_id)
    finalist.participation_confirmed = True
    finalist.participation_confirmed_at = now_moscow()
    await db_session.commit()

    if not was_all_confirmed and await all_finalists_confirmed(db_session, finalist.tournament_id):
        await notify_admins_all_finalists_confirmed(
            callback.bot,
            db_session,
            finalist.tournament_id,
        )

    await callback.message.edit_text("✅ Участие в финале подтверждено. Ожидайте дальнейших инструкций.")


async def _load_declinable_finalist(
    db_session: AsyncSession,
    finalist_id: int,
    telegram_id: int,
):
    from db.models import Finalist

    return (
        await db_session.execute(
            select(Finalist, Registration)
            .join(Registration, Registration.id == Finalist.registration_id)
            .join(User, User.id == Registration.user_id)
            .where(
                Finalist.id == finalist_id,
                User.telegram_id == telegram_id,
            )
        )
    ).first()


@user_router.callback_query(F.data.regexp(r"^decline_finalist_\d+$"))
async def decline_finalist(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.finalists import decline_finalist_confirm_keyboard

    await callback.answer()
    finalist_id = int(callback.data.split("_")[-1])
    row = await _load_declinable_finalist(db_session, finalist_id, callback.from_user.id)
    if not row:
        await callback.message.edit_text("❌ Запись финалиста не найдена.")
        return

    _finalist, registration = row
    if _finalist.participation_confirmed:
        await callback.message.edit_text("ℹ️ Вы уже подтвердили участие в финале.")
        return

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == _finalist.tournament_id))
    ).scalar_one_or_none()
    if not tour or tour.status not in (
        TournamentStatus.FINALISTS_SELECTED,
        TournamentStatus.FINAL_IN_PROGRESS,
    ):
        await callback.message.edit_text("🚫 Отказ от финала сейчас недоступен.")
        return

    await callback.message.edit_text(
        f"⚠️ Вы уверены, что хотите отказаться от участия в финале турнира «{tour.title}»?\n"
        f"Riot ID: {registration.game_nick}",
        reply_markup=decline_finalist_confirm_keyboard(finalist_id),
    )


@user_router.callback_query(F.data.startswith("decline_finalist_confirm_"))
async def decline_finalist_confirm(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    finalist_id = int(callback.data.split("_")[-1])
    row = await _load_declinable_finalist(db_session, finalist_id, callback.from_user.id)
    if not row:
        await callback.message.edit_text("❌ Запись финалиста не найдена.")
        return

    finalist, registration = row
    if finalist.participation_confirmed:
        await callback.message.edit_text("ℹ️ Вы уже подтвердили участие в финале.")
        return

    from bot.services.final_stage import notify_admins_finalist_declined

    finalist.participation_confirmed = False
    finalist.participation_confirmed_at = None
    await db_session.commit()
    await notify_admins_finalist_declined(
        callback.bot, db_session, finalist.tournament_id, registration
    )
    await callback.message.edit_text(
        "❌ Вы отказались от участия в финале. Администратор может назначить замену."
    )


@user_router.callback_query(F.data.startswith("decline_finalist_cancel_"))
async def decline_finalist_cancel(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.finalists import confirm_finalist_keyboard

    await callback.answer()
    finalist_id = int(callback.data.split("_")[-1])
    row = await _load_declinable_finalist(db_session, finalist_id, callback.from_user.id)
    if not row:
        await callback.message.edit_text("❌ Запись финалиста не найдена.")
        return

    finalist, _registration = row
    if finalist.participation_confirmed:
        await callback.message.edit_text("ℹ️ Вы уже подтвердили участие в финале.")
        return

    await callback.message.edit_text(
        "🏅 Подтвердите участие в финале:",
        reply_markup=confirm_finalist_keyboard(finalist_id),
    )


def _withdraw_confirm_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Да, отозвать заявку", callback_data="withdraw_confirm")],
        [types.InlineKeyboardButton(text="❌ Отмена", callback_data="withdraw_cancel")],
    ])


@user_router.callback_query(F.data == "withdraw_cancel")
async def cmd_withdraw_cancel(
    callback: types.CallbackQuery,
    role: str,
    state: FSMContext,
    db_session: AsyncSession,
):
    await callback.answer()
    await callback.message.edit_text(
        "👋 Добро пожаловать на турнир по Valorant!\nВыберите действие:",
        reply_markup=await _build_user_home_markup(
            db_session, callback.from_user.id, role, state
        ),
    )


@user_router.callback_query(F.data == "withdraw_confirm")
async def cmd_withdraw_confirm(
    callback: types.CallbackQuery,
    role: str,
    state: FSMContext,
    db_session: AsyncSession,
):
    await callback.answer()

    query = select(Registration).join(User).where(
        User.telegram_id == callback.from_user.id,
        Registration.status == RegistrationStatus.REGISTERED,
    ).order_by(Registration.id.desc()).limit(1)
    reg = (await db_session.execute(query)).scalar_one_or_none()

    if not reg:
        await callback.message.edit_text("❌ Активная заявка не найдена.")
        return

    tour = (await db_session.execute(
        select(Tournament).where(Tournament.id == reg.tournament_id)
    )).scalar_one_or_none()

    if not tour or tour.status not in (
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
    ):
        await callback.message.edit_text(
            "🚫 Отозвать заявку уже нельзя: отбор участников начался или турнир завершён."
        )
        return

    reg.status = RegistrationStatus.WITHDRAWN
    await db_session.commit()

    await callback.message.edit_text(
        f"✅ Заявка на турнир «{tour.title}» успешно отозвана.\n\n"
        "👋 Добро пожаловать на турнир по Valorant!\nВыберите действие:",
        reply_markup=await _build_user_home_markup(
            db_session, callback.from_user.id, role, state
        ),
    )


# --- РЕДАКТИРОВАНИЕ ПРОФИЛЯ ПОЛЬЗОВАТЕЛЯ ---
@user_router.callback_query(
    ProfileEditStates.waiting_for_field_choice,
    F.data == "profile_edit_nick",
)
async def cmd_edit_profile_choose_nick(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    await callback.answer()
    state_data = await state.get_data()
    reg_id = state_data.get("edit_reg_id")
    if not reg_id:
        await state.clear()
        await callback.message.edit_text("❌ Сессия редактирования истекла. Нажмите «✏️ Редактировать профиль» снова.")
        return

    reg = await _load_editable_registration(db_session, reg_id)
    if not reg:
        await state.clear()
        await callback.message.edit_text("❌ Активная заявка не найдена.")
        return

    await callback.message.edit_text(
        f"📝 Текущий Riot ID: {reg.game_nick}\n"
        f"Введите новый Riot ID в формате Название#тег (например, Player#EUW):"
    )
    await state.set_state(ProfileEditStates.waiting_for_new_nick)


@user_router.callback_query(
    ProfileEditStates.waiting_for_field_choice,
    F.data == "profile_edit_rank",
)
async def cmd_edit_profile_choose_rank(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    await callback.answer()
    state_data = await state.get_data()
    reg_id = state_data.get("edit_reg_id")
    if not reg_id:
        await state.clear()
        await callback.message.edit_text("❌ Сессия редактирования истекла. Нажмите «✏️ Редактировать профиль» снова.")
        return

    reg = await _load_editable_registration(db_session, reg_id)
    if not reg:
        await state.clear()
        await callback.message.edit_text("❌ Активная заявка не найдена.")
        return

    await callback.message.edit_text(
        f"🏅 Текущий ранг: {reg.game_rank}\n\nВыберите новый ранг:",
        reply_markup=get_ranks_keyboard(callback_prefix="profile_rank"),
    )
    await state.set_state(ProfileEditStates.waiting_for_new_rank)


@user_router.callback_query(
    ProfileEditStates.waiting_for_new_rank,
    F.data.startswith("profile_rank_"),
)
async def cmd_edit_profile_pick_rank(
    callback: types.CallbackQuery,
    state: FSMContext,
    role: str,
    db_session: AsyncSession,
):
    await callback.answer()
    selected_rank = callback.data.split("_", maxsplit=2)[-1].capitalize()

    if selected_rank == "Radiant":
        state_data = await state.get_data()
        reg_id = state_data.get("edit_reg_id")
        if not reg_id:
            await state.clear()
            await callback.message.edit_text("❌ Сессия редактирования истекла.")
            return

        reg = await _load_editable_registration(db_session, reg_id)
        if not reg:
            await state.clear()
            await callback.message.edit_text("❌ Активная заявка не найдена.")
            return

        reg.game_rank = selected_rank
        await db_session.commit()
        await _finish_profile_edit(
            callback.message,
            state,
            role,
            db_session,
            f"✅ Ранг успешно обновлён: {selected_rank}",
            telegram_id=callback.from_user.id,
        )
        return

    await state.update_data(main_rank=selected_rank)
    await callback.message.edit_text(
        f"Вы выбрали ранг: {selected_rank}\nВыберите ступень:",
        reply_markup=get_tiers_keyboard(selected_rank, callback_prefix="profile_tier"),
    )
    await state.set_state(ProfileEditStates.waiting_for_new_rank_tier)


@user_router.callback_query(
    ProfileEditStates.waiting_for_new_rank_tier,
    F.data.startswith("profile_tier_"),
)
async def cmd_edit_profile_save_rank(
    callback: types.CallbackQuery,
    state: FSMContext,
    role: str,
    db_session: AsyncSession,
):
    await callback.answer()
    parts = callback.data.split("_")
    full_rank = f"{parts[2].capitalize()} {parts[3]}"

    state_data = await state.get_data()
    reg_id = state_data.get("edit_reg_id")
    if not reg_id:
        await state.clear()
        await callback.message.edit_text("❌ Сессия редактирования истекла. Нажмите «✏️ Редактировать профиль» снова.")
        return

    reg = await _load_editable_registration(db_session, reg_id)
    if not reg:
        await state.clear()
        await callback.message.edit_text("❌ Активная заявка не найдена.")
        return

    reg.game_rank = full_rank
    await db_session.commit()
    await _finish_profile_edit(
        callback.message,
        state,
        role,
        db_session,
        f"✅ Ранг успешно обновлён: {full_rank}",
        telegram_id=callback.from_user.id,
    )


@user_router.message(ProfileEditStates.waiting_for_new_nick)
async def cmd_edit_profile_save_nick(
    message: types.Message,
    state: FSMContext,
    role: str,
    db_session: AsyncSession,
):
    riot_id = message.text.strip()
    if not _is_valid_riot_id(riot_id):
        await message.answer(
            "❌ Неверный формат Riot ID. Пожалуйста, введите в формате Название#тег (например, Player#EUW):"
        )
        return

    state_data = await state.get_data()
    reg_id = state_data.get("edit_reg_id")
    if not reg_id:
        await state.clear()
        await message.answer("❌ Сессия редактирования истекла. Нажмите «✏️ Редактировать профиль» снова.")
        return

    reg = (await db_session.execute(
        select(Registration).where(
            Registration.id == reg_id,
            Registration.status.in_(
                (
                    RegistrationStatus.REGISTERED,
                    RegistrationStatus.SELECTED_MAIN,
                    RegistrationStatus.SELECTED_RESERVE,
                )
            ),
        )
    )).scalar_one_or_none()

    if not reg:
        await state.clear()
        await message.answer("❌ Активная заявка не найдена.")
        return

    user_display = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
    reg.game_nick = riot_id
    reg.contact_telegram = user_display
    await db_session.commit()
    await _finish_profile_edit(
        message,
        state,
        role,
        db_session,
        f"✅ Riot ID успешно обновлён: {riot_id}",
    )