# bot/handlers/admin.py
import secrets
from aiogram import Router, types, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, delete

from bot.filters.role import RoleFilter
from bot.utils.callback_ui import reply_or_edit
from bot.utils.timezone import format_moscow, format_moscow_date, now_moscow
from bot.states.admin import AdminTournamentStates
from bot.services.tournament_cleanup import delete_tournament_cascade
from bot.keyboards.menu import append_developer_mode_toggle, clear_reply_keyboard
from bot.services.grouping import (
    format_tournament_groups_text,
    get_main_roster,
    get_unconfirmed_players,
    all_main_roster_confirmed,
    notify_admins_all_main_roster_confirmed,
    tournament_has_groups,
    get_tournament_groups,
    move_member_between_groups,
    format_single_group_text,
    get_group_members,
    resolve_group_capacity,
)
from bot.services.selection import (
    apply_selection_draft,
    build_random_draft,
    draft_preview_keyboard,
    format_draft_text,
    get_selection_draft,
    save_selection_draft,
    swap_registration_in_draft,
)
from bot.services.rules import (
    admin_rules_menu_keyboard,
    extract_rules_url,
    get_global_rules_url,
    set_global_rules_url,
)
from bot.services.participant_lists import format_participant_lists_text
from bot.services.replacements import (
    OUTSIDE_MATCH_REASON,
    add_member_to_group,
    get_available_reserves,
    get_outside_roster_candidates,
    remove_group_member,
    replace_group_member,
    replacement_followup_keyboard,
)
from bot.utils.telegram_contact import resolve_telegram_contact
from bot.services.tournament_history import (
    format_archive_list_text,
    format_tournament_history_detail,
    get_completed_tournaments,
)
from bot.services.stages import ensure_group_stage_started
from config import settings
from db.models import Tournament, TournamentStatus, Admin, AdminRole, AdminStatus, Registration, RegistrationStatus, User, SubscriptionStatus
from db.models import TournamentGroup, GroupMember, TournamentSetting

admin_router = Router()

admin_router.message.filter(RoleFilter("admin", "developer"))
admin_router.callback_query.filter(RoleFilter("admin", "developer"))

# Дополнительные состояния
class SelectionStates(StatesGroup):
    waiting_for_main_count = State()
    waiting_for_reserve_count = State()


class SelectionEditStates(StatesGroup):
    waiting_for_move_target = State()

class DeveloperStates(StatesGroup):
    waiting_for_new_admin_id = State()


def _is_developer(role: str) -> bool:
    return role == "developer"


async def _deny_unless_developer(
    event: types.Message | types.CallbackQuery,
    role: str,
) -> bool:
    if _is_developer(role):
        return True
    text = "❌ Доступно только разработчику."
    if isinstance(event, types.CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)
    return False


async def _reply_or_edit(callback: types.CallbackQuery, text: str, reply_markup: types.InlineKeyboardMarkup):
    await reply_or_edit(callback, text, reply_markup)


async def _build_admin_home_markup(role: str) -> types.InlineKeyboardMarkup:
    buttons = [
        [types.InlineKeyboardButton(text="🏆 Управление турнирами", callback_data="admin_tour_list")],
        [types.InlineKeyboardButton(text="📜 История турниров", callback_data="admin_history")],
        [types.InlineKeyboardButton(text="📜 Управление регламентом", callback_data="admin_rules_menu")],
    ]
    if _is_developer(role):
        buttons.append([types.InlineKeyboardButton(text="👥 Управление админами", callback_data="dev_manage_admins")])
        buttons.append([types.InlineKeyboardButton(text="🧹 Очистить историю турниров", callback_data="dev_clear_history")])
        append_developer_mode_toggle(buttons, role, admin_mode=True)
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def build_admin_home_content(
    db_session: AsyncSession,
    role: str,
    telegram_id: int,
) -> tuple[str, types.InlineKeyboardMarkup] | None:
    admin_user = (
        await db_session.execute(
            select(Admin).where(Admin.telegram_id == telegram_id)
        )
    ).scalar_one_or_none()

    if not admin_user and not _is_developer(role):
        return None

    role_label = admin_user.role.value if admin_user else "developer"
    text = f"👑 Панель управления турнирами\nВаша роль: {role_label}"
    if _is_developer(role):
        text += "\n\n🛠 Инструменты разработчика доступны ниже."
    return text, await _build_admin_home_markup(role)


async def send_admin_home(message: types.Message, db_session: AsyncSession, role: str) -> None:
    content = await build_admin_home_content(db_session, role, message.from_user.id)
    if not content:
        await message.answer("❌ У вас нет прав администратора.")
        return
    text, markup = content
    await message.answer(text, reply_markup=markup)


# --- ГЛАВНОЕ МЕНЮ ---
@admin_router.message(CommandStart())
async def admin_cmd_start(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
    role: str,
):
    await state.clear()
    await send_admin_home(message, db_session, role)


@admin_router.message(Command("admin"))
async def cmd_admin_menu(message: types.Message, db_session: AsyncSession, role: str = "user"):
    await send_admin_home(message, db_session, role)


@admin_router.callback_query(F.data == "admin_home")
async def admin_home_callback(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    await callback.answer()
    admin_user = (
        await db_session.execute(
            select(Admin).where(Admin.telegram_id == callback.from_user.id)
        )
    ).scalar_one_or_none()
    if not admin_user and not _is_developer(role):
        await callback.message.answer("❌ У вас нет прав администратора.")
        return

    role_label = admin_user.role.value if admin_user else "developer"
    text = f"👑 Панель управления турнирами\nВаша роль: {role_label}"
    if _is_developer(role):
        text += "\n\n🛠 Инструменты разработчика доступны ниже."
    await _reply_or_edit(callback, text, await _build_admin_home_markup(role))


# --- ИСТОРИЯ ТУРНИРОВ ---
@admin_router.callback_query(F.data == "admin_history")
async def admin_history_list(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tournaments = await get_completed_tournaments(db_session)
    text = await format_archive_list_text(db_session, admin_view=True)
    if len(tournaments) > 1:
        text += "\n\nВыберите турнир для подробностей."

    buttons: list[list[types.InlineKeyboardButton]] = []
    for tour in tournaments:
        date_line = format_moscow_date(tour.completed_at) if tour.completed_at else "—"
        buttons.append([types.InlineKeyboardButton(
            text=f"{tour.title} ({date_line})",
            callback_data=f"admin_history_tour_{tour.id}",
        )])
    buttons.append([types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_home")])
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@admin_router.callback_query(F.data.startswith("admin_history_tour_"))
async def admin_history_detail(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    text = await format_tournament_history_detail(db_session, tour_id, admin_view=True)
    buttons = [
        [types.InlineKeyboardButton(
            text="📊 Рейтинг группового этапа",
            callback_data=f"tour_stages_{tour_id}",
        )],
        [types.InlineKeyboardButton(
            text="⚙️ Управление турниром",
            callback_data=f"manage_tour_{tour_id}",
        )],
        [types.InlineKeyboardButton(text="⬅️ К архиву", callback_data="admin_history")],
        [types.InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_home")],
    ]
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# --- СПИСОК ТУРНИРОВ ---
@admin_router.callback_query(F.data == "admin_tour_list")
async def view_tournaments_list(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    query = select(Tournament).order_by(Tournament.id.desc())
    res = await db_session.execute(query)
    tournaments = res.scalars().all()

    text = "🏆 Список турниров:"
    buttons = []
    
    status_translations = {
        TournamentStatus.DRAFT: "Черновик",
        TournamentStatus.REGISTRATION_OPEN: "Регистрация открыта",
        TournamentStatus.REGISTRATION_CLOSED: "Регистрация закрыта",
        TournamentStatus.SELECTION_DONE: "Отбор завершен",
        TournamentStatus.CONFIRMATION_PENDING: "Ожидание подтверждений",
        TournamentStatus.GROUPS_FORMED: "Группы сформированы",
        TournamentStatus.STAGE_IN_PROGRESS: "Групповой этап",
        TournamentStatus.RATING_CALCULATED: "Рейтинг подсчитан",
        TournamentStatus.FINALISTS_SELECTED: "Финалисты определены",
        TournamentStatus.FINAL_IN_PROGRESS: "Финал",
        TournamentStatus.COMPLETED: "Завершен",
        TournamentStatus.CANCELLED: "Отменен"
    }

    for t in tournaments:
        status_ru = status_translations.get(t.status, t.status.value)
        buttons.append([types.InlineKeyboardButton(
            text=f"{t.title} ({status_ru})", 
            callback_data=f"manage_tour_{t.id}"
        )])

    buttons.append([types.InlineKeyboardButton(text="➕ Создать новый турнир", callback_data="admin_create_tour")])
    buttons.append([types.InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_home")])
    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))


# --- УПРАВЛЕНИЕ КОНКРЕТНЫМ ТУРНИРОМ ---
@admin_router.callback_query(F.data.startswith("manage_tour_"))
async def manage_single_tournament(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    role: str,
    *,
    notice: str | None = None,
):
    if callback.id != "0":
        await callback.answer()
    tour_id = int(callback.data.split("_")[2])

    query = select(Tournament).where(Tournament.id == tour_id)
    res = await db_session.execute(query)
    tour = res.scalar_one_or_none()

    if not tour:
        await callback.message.edit_text("Турнир не найден.")
        return

    is_dev = _is_developer(role)

    query_reg = select(func.count(Registration.id)).where(
        Registration.tournament_id == tour_id,
        Registration.status.in_(
            (
                RegistrationStatus.REGISTERED,
                RegistrationStatus.SELECTED_MAIN,
                RegistrationStatus.SELECTED_RESERVE,
            )
        ),
    )
    res_reg = await db_session.execute(query_reg)
    reg_count = res_reg.scalar() or 0

    registered_count = await db_session.scalar(
        select(func.count(Registration.id)).where(
            Registration.tournament_id == tour_id,
            Registration.status == RegistrationStatus.REGISTERED,
        )
    ) or 0

    status_ru = {
        TournamentStatus.DRAFT: "Черновик",
        TournamentStatus.REGISTRATION_OPEN: "Регистрация открыта",
        TournamentStatus.REGISTRATION_CLOSED: "Регистрация закрыта",
        TournamentStatus.SELECTION_DONE: "Отбор завершен",
        TournamentStatus.CONFIRMATION_PENDING: "Ожидание подтверждений",
        TournamentStatus.GROUPS_FORMED: "Группы сформированы",
        TournamentStatus.STAGE_IN_PROGRESS: "Групповой этап",
        TournamentStatus.RATING_CALCULATED: "Рейтинг подсчитан",
        TournamentStatus.FINALISTS_SELECTED: "Финалисты определены",
        TournamentStatus.FINAL_IN_PROGRESS: "Финал",
        TournamentStatus.COMPLETED: "Завершен",
        TournamentStatus.CANCELLED: "Отменен"
    }.get(tour.status, tour.status.value)

    text = f"🏆 Управление турниром: {tour.title}\n📊 Статус: {status_ru}\n👥 Заявок: {reg_count}"

    if tour.status == TournamentStatus.CONFIRMATION_PENDING:
        main_players = await get_main_roster(db_session, tour_id)
        confirmed_count = sum(1 for player in main_players if player.participation_confirmed)
        text += f"\n✅ Подтверждено участие: {confirmed_count}/{len(main_players)}"

    buttons = []
    if tour.status == TournamentStatus.DRAFT:
        buttons.append([types.InlineKeyboardButton(text="🟢 Открыть регистрацию", callback_data=f"tour_open_{tour_id}")])
        buttons.append([types.InlineKeyboardButton(text="🗑 Удалить турнир", callback_data=f"tour_delete_confirm_{tour_id}")])
        if is_dev:
            buttons.append([types.InlineKeyboardButton(text="🤖 Залить 25 ботов", callback_data=f"test_fill_{tour_id}")])
    elif tour.status == TournamentStatus.REGISTRATION_OPEN:
        buttons.append([types.InlineKeyboardButton(text="🔴 Закрыть регистрацию", callback_data=f"tour_close_{tour_id}")])
        if is_dev:
            buttons.append([types.InlineKeyboardButton(text="🤖 Залить 25 ботов", callback_data=f"test_fill_{tour_id}")])
    elif tour.status == TournamentStatus.REGISTRATION_CLOSED:
        buttons.append([types.InlineKeyboardButton(text="🟢 Открыть регистрацию", callback_data=f"tour_open_{tour_id}")])
        if registered_count > 0:
            buttons.append([types.InlineKeyboardButton(text="🎲 Провести отбор", callback_data=f"start_manual_select_{tour_id}")])
        if is_dev:
            buttons.append([types.InlineKeyboardButton(text="🤖 Залить 25 ботов", callback_data=f"test_fill_{tour_id}")])
    elif tour.status == TournamentStatus.SELECTION_DONE:
        buttons.append([types.InlineKeyboardButton(text="📨 Начать сбор подтверждений", callback_data=f"tour_start_confirm_{tour_id}")])
        buttons.append([types.InlineKeyboardButton(text="⚙️ Редактировать состав", callback_data=f"tour_edit_members_{tour_id}")])
        if is_dev:
            buttons.append([types.InlineKeyboardButton(text="🧪 Подтвердить все заявки", callback_data=f"test_confirm_all_{tour_id}")])
    elif tour.status == TournamentStatus.CONFIRMATION_PENDING:
        buttons.append([types.InlineKeyboardButton(text="⚙️ Редактировать состав", callback_data=f"tour_edit_members_{tour_id}")])
        buttons.append([types.InlineKeyboardButton(text="👥 Сформировать группы", callback_data=f"tour_build_groups_{tour_id}")])
        if is_dev:
            buttons.append([types.InlineKeyboardButton(text="🧪 Подтвердить все заявки", callback_data=f"test_confirm_all_{tour_id}")])
    elif tour.status == TournamentStatus.GROUPS_FORMED:
        groups_count = await db_session.scalar(
            select(func.count(TournamentGroup.id)).where(TournamentGroup.tournament_id == tour_id)
        ) or 0
        text += f"\n👥 Сформировано групп: {groups_count}"
        buttons.append([types.InlineKeyboardButton(text="📋 Списки групп", callback_data=f"tour_view_groups_{tour_id}")])
        buttons.append([types.InlineKeyboardButton(text="📊 К игровым этапам", callback_data=f"tour_stages_{tour_id}")])
    elif tour.status == TournamentStatus.STAGE_IN_PROGRESS:
        buttons.append([types.InlineKeyboardButton(text="📊 Групповой этап", callback_data=f"tour_stages_{tour_id}")])
        buttons.append([types.InlineKeyboardButton(text="📋 Списки групп", callback_data=f"tour_view_groups_{tour_id}")])
    elif tour.status == TournamentStatus.RATING_CALCULATED:
        buttons.append([types.InlineKeyboardButton(text="📊 Итоговый рейтинг", callback_data=f"tour_stages_{tour_id}")])
        buttons.append([types.InlineKeyboardButton(text="🏅 Определить финалистов", callback_data=f"finalists_select_{tour_id}")])
    elif tour.status == TournamentStatus.FINALISTS_SELECTED:
        from bot.services.final_stage import all_finalists_confirmed, get_finalists_confirmation_stats

        confirmed, total = await get_finalists_confirmation_stats(db_session, tour_id)
        text += f"\n✅ Финалисты подтвердили: {confirmed}/{total}"
        all_confirmed = await all_finalists_confirmed(db_session, tour_id)
        buttons.append([types.InlineKeyboardButton(text="📋 Финалисты и рейтинг", callback_data=f"finalists_view_{tour_id}")])
        if not all_confirmed:
            buttons.append([types.InlineKeyboardButton(text="📨 Запросить подтверждение финалистов", callback_data=f"finalists_confirm_send_{tour_id}")])
            buttons.append([types.InlineKeyboardButton(text="♻️ Заменить финалиста", callback_data=f"finalists_replace_menu_{tour_id}")])
            if is_dev:
                buttons.append([types.InlineKeyboardButton(text="🧪 Подтвердить всех финалистов", callback_data=f"test_confirm_finalists_{tour_id}")])
        if all_confirmed:
            buttons.append([types.InlineKeyboardButton(text="🏆 Управление финалом", callback_data=f"final_dash_{tour_id}")])
    elif tour.status == TournamentStatus.FINAL_IN_PROGRESS:
        from bot.services.tournament_meta import get_tournament_meta

        meta = await get_tournament_meta(db_session, tour_id)
        buttons.append([types.InlineKeyboardButton(text="🏆 Управление финалом", callback_data=f"final_dash_{tour_id}")])
        buttons.append([types.InlineKeyboardButton(text="📋 Финалисты и рейтинг", callback_data=f"finalists_view_{tour_id}")])
    elif tour.status == TournamentStatus.COMPLETED:
        from bot.services.final_stage import format_final_summary_text

        final_summary = await format_final_summary_text(db_session, tour_id, admin_view=True)
        if final_summary:
            text += final_summary
        buttons.append([types.InlineKeyboardButton(
            text="📜 Подробная история",
            callback_data=f"admin_history_tour_{tour_id}",
        )])
        buttons.append([types.InlineKeyboardButton(text="📊 Рейтинг группового этапа", callback_data=f"tour_stages_{tour_id}")])

    if tour.status != TournamentStatus.COMPLETED:
        buttons.append([types.InlineKeyboardButton(text="🗑 Удалить турнир", callback_data=f"tour_delete_confirm_{tour_id}")])

    show_participant_lists = tour.status in (
        TournamentStatus.DRAFT,
        TournamentStatus.REGISTRATION_OPEN,
        TournamentStatus.REGISTRATION_CLOSED,
        TournamentStatus.SELECTION_DONE,
        TournamentStatus.CONFIRMATION_PENDING,
        TournamentStatus.GROUPS_FORMED,
        TournamentStatus.STAGE_IN_PROGRESS,
        TournamentStatus.RATING_CALCULATED,
        TournamentStatus.FINALISTS_SELECTED,
        TournamentStatus.FINAL_IN_PROGRESS,
    )
    if tour.status not in (TournamentStatus.COMPLETED, TournamentStatus.CANCELLED):
        buttons.append([types.InlineKeyboardButton(
            text="➕ Добавить участника",
            callback_data=f"admin_add_player_{tour_id}",
        )])
    if reg_count > 0 and show_participant_lists:
        buttons.append([types.InlineKeyboardButton(text="📋 Списки участников", callback_data=f"tour_lists_{tour_id}")])
        if is_dev:
            buttons.append([types.InlineKeyboardButton(text="🧹 Очистить заявки", callback_data=f"test_clear_{tour_id}")])
    elif reg_count > 0 and is_dev and tour.status not in (TournamentStatus.COMPLETED,):
        buttons.append([types.InlineKeyboardButton(text="🧹 Очистить заявки", callback_data=f"test_clear_{tour_id}")])

    buttons.append([types.InlineKeyboardButton(text="⬅️ К списку турниров", callback_data="admin_tour_list")])
    if notice:
        text = f"{notice}\n\n{text}"
    await _reply_or_edit(
        callback,
        text,
        types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


def _rules_menu_text(rules_url: str | None) -> str:
    text = "📜 Управление регламентом\n\n"
    if rules_url:
        text += f"Текущая ссылка:\n{rules_url}"
    else:
        text += "Ссылка на регламент ещё не задана."
    return text


async def _show_rules_menu(
    event: types.CallbackQuery | types.Message,
    db_session: AsyncSession,
) -> None:
    rules_url = await get_global_rules_url(db_session)
    text = _rules_menu_text(rules_url)
    markup = admin_rules_menu_keyboard(rules_url)
    if isinstance(event, types.CallbackQuery):
        await _reply_or_edit(event, text, markup)
    else:
        await event.answer(text, reply_markup=markup)


@admin_router.callback_query(F.data.in_({"admin_rules_menu", "admin_edit_rules"}))
async def admin_rules_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    await _show_rules_menu(callback, db_session)


@admin_router.callback_query(F.data == "admin_rules_view_missing")
async def admin_rules_view_missing(callback: types.CallbackQuery):
    await callback.answer("Регламент ещё не задан. Используйте «Редактирование регламента».", show_alert=True)


@admin_router.callback_query(F.data == "admin_rules_edit_link")
async def admin_rules_edit_link_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "✏️ Отправьте ссылку на сообщение с регламентом в Telegram-канале.\n\n"
        "Пример: https://t.me/your_channel/123\n"
        "Для отмены: /admin"
    )
    await state.set_state(AdminTournamentStates.waiting_for_rules_url)


@admin_router.message(AdminTournamentStates.waiting_for_rules_url)
async def admin_rules_url_save(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
):
    if not message.text:
        await message.answer("❌ Отправьте ссылку на сообщение с регламентом.")
        return

    try:
        rules_url = extract_rules_url(message.text)
        await set_global_rules_url(db_session, rules_url)
        await db_session.commit()
    except ValueError:
        await message.answer(
            "❌ Неверная ссылка. Нужна ссылка на конкретное сообщение в Telegram.\n"
            "Пример: https://t.me/your_channel/123"
        )
        return

    await state.clear()
    await message.answer("✅ Ссылка на регламент сохранена.")
    await _show_rules_menu(message, db_session)


@admin_router.callback_query(F.data.startswith("tour_edit_rules_"))
async def tour_edit_rules_legacy_redirect(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
):
    await callback.answer("Регламент задаётся ссылкой на сообщение в канале.")
    await _show_rules_menu(callback, db_session)

# --- СОЗДАНИЕ ТУРНИРА (FSM) ---
@admin_router.callback_query(F.data == "admin_create_tour")
async def start_create_tournament(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("📝 Введите название для нового турнира:")
    await state.set_state(AdminTournamentStates.waiting_for_title)

@admin_router.message(AdminTournamentStates.waiting_for_title)
async def process_tournament_title(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
    role: str,
):
    title = message.text.strip()
    if not title:
        await message.answer("❌ Название не может быть пустым. Введите название:")
        return

    admin_user = (
        await db_session.execute(
            select(Admin).where(Admin.telegram_id == message.from_user.id)
        )
    ).scalar_one_or_none()

    channel_username = None
    try:
        chat = await message.bot.get_chat(settings.CHANNEL_ID)
        if chat.username:
            channel_username = f"@{chat.username}"
    except Exception as exc:
        from loguru import logger
        logger.warning(
            f"Could not fetch tournament channel info for {settings.CHANNEL_ID}: {exc}. "
            "Add the bot as a channel administrator so subscription checks work."
        )
        if settings.CHANNEL_USERNAME:
            channel_username = (
                settings.CHANNEL_USERNAME
                if settings.CHANNEL_USERNAME.startswith("@")
                else f"@{settings.CHANNEL_USERNAME}"
            )

    new_tour = Tournament(
        title=title,
        channel_id=settings.CHANNEL_ID,
        channel_username=channel_username,
        status=TournamentStatus.REGISTRATION_CLOSED,
        main_slots=10,
        reserve_slots=0,
        created_by=admin_user.id if admin_user else None,
    )
    db_session.add(new_tour)
    await db_session.flush()

    db_session.add(TournamentSetting(tournament_id=new_tour.id))
    await db_session.commit()
    await state.clear()

    await message.answer(f"✅ Турнир «{title}» создан.")
    fake = types.CallbackQuery(
        id="0",
        from_user=message.from_user,
        chat_instance="0",
        message=message,
        data=f"manage_tour_{new_tour.id}",
    )
    await manage_single_tournament(fake, db_session, role)


# --- ОТКРЫТИЕ И ЗАКРЫТИЕ РЕГИСТРАЦИИ ---
@admin_router.callback_query(F.data.startswith("tour_open_"))
async def action_open_registration(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    await callback.answer()
    tour_id = int(callback.data.split("_")[2])
    
    tour = (await db_session.execute(select(Tournament).where(Tournament.id == tour_id))).scalar_one_or_none()
    notice = None
    if tour:
        tour.status = TournamentStatus.REGISTRATION_OPEN
        await db_session.commit()
        notice = "🟢 Регистрация на турнир успешно открыта!"
    await manage_single_tournament(callback, db_session, role, notice=notice)

@admin_router.callback_query(F.data.startswith("tour_close_"))
async def action_close_registration(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    await callback.answer()
    tour_id = int(callback.data.split("_")[2])
    
    tour = (await db_session.execute(select(Tournament).where(Tournament.id == tour_id))).scalar_one_or_none()
    notice = None
    if tour:
        tour.status = TournamentStatus.REGISTRATION_CLOSED
        await db_session.commit()
        notice = "🔴 Регистрация на турнир закрыта!"
    await manage_single_tournament(callback, db_session, role, notice=notice)


# --- ПЕРЕВОД В РЕЖИМ СБОРА ПОДТВЕРЖДЕНИЙ ---
@admin_router.callback_query(F.data.startswith("tour_start_confirm_"))
async def action_start_confirmation(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])

    tour = (await db_session.execute(select(Tournament).where(Tournament.id == tour_id))).scalar_one_or_none()
    if not tour:
        return

    main_players = await get_main_roster(db_session, tour_id)
    tour.status = TournamentStatus.CONFIRMATION_PENDING
    await db_session.commit()

    sent = 0
    from bot.services.replacements import confirm_participation_keyboard

    for player in main_players:
        user = (
            await db_session.execute(select(User).where(User.id == player.user_id))
        ).scalar_one_or_none()
        if not user:
            continue
        try:
            await callback.bot.send_message(
                user.telegram_id,
                "📨 Вы попали в основной состав турнира!\n"
                "Подтвердите готовность участвовать:",
                reply_markup=confirm_participation_keyboard(player.id),
            )
            sent += 1
        except Exception:
            pass

    notice = (
        f"📨 Турнир переведен в статус: Ожидание подтверждений.\n"
        f"Запрос отправлен {sent} участникам основного состава."
    )
    await manage_single_tournament(callback, db_session, role, notice=notice)


# --- РЕДАКТИРОВАНИЕ СОСТАВА И ЗАМЕНЫ ---
@admin_router.callback_query(F.data.startswith("tour_edit_members_"))
async def menu_edit_tournament_members(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    *,
    notice: str | None = None,
):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])

    query_main = select(Registration).where(
        Registration.tournament_id == tour_id,
        Registration.status == RegistrationStatus.SELECTED_MAIN
    )
    main_players = (await db_session.execute(query_main)).scalars().all()
    reserves = (
        await db_session.execute(
            select(Registration).where(
                Registration.tournament_id == tour_id,
                Registration.status == RegistrationStatus.SELECTED_RESERVE,
            ).order_by(Registration.id.asc())
        )
    ).scalars().all()
    unconfirmed = [p for p in main_players if not p.participation_confirmed]

    text = (
        "⚙️ Управление составом:\n\n"
        "🟢 Основной состав — нажмите, чтобы исключить или заменить.\n"
    )
    if unconfirmed:
        text += f"⏳ Не подтвердили участие: {len(unconfirmed)}\n"

    buttons = []
    for p in main_players:
        status_confirm = "✅" if p.participation_confirmed else "⏳"
        buttons.append([types.InlineKeyboardButton(
            text=f"❌ {p.contact_telegram} ({p.game_nick}) {status_confirm}",
            callback_data=f"reg_kick_{p.id}_{tour_id}",
        )])

    if reserves:
        text += f"\n🔵 Резерв ({len(reserves)}):\n"
        for reserve in reserves[:8]:
            text += f"- {reserve.contact_telegram} ({reserve.game_nick})\n"
        if len(reserves) > 8:
            text += f"... и ещё {len(reserves) - 8}\n"

    if unconfirmed and reserves:
        buttons.append([types.InlineKeyboardButton(
            text="♻️ Заменить не подтвердивших",
            callback_data=f"reg_replace_unconfirmed_{tour_id}",
        )])

    buttons.append([types.InlineKeyboardButton(text="⬅️ Назад в управление", callback_data=f"manage_tour_{tour_id}")])
    if notice:
        text = f"{notice}\n\n{text}"
    await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))


# --- ИСКЛЮЧЕНИЕ / РУЧНАЯ ЗАМЕНА РЕЗЕРВИСТОМ ---
@admin_router.callback_query(F.data.startswith("reg_kick_"))
async def action_kick_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    reg_id = int(parts[2])
    tour_id = int(parts[3])

    kicked_reg = (
        await db_session.execute(select(Registration).where(Registration.id == reg_id))
    ).scalar_one_or_none()
    if not kicked_reg:
        await callback.message.answer("❌ Участник не найден.")
        return

    reserves = (
        await db_session.execute(
            select(Registration).where(
                Registration.tournament_id == tour_id,
                Registration.status == RegistrationStatus.SELECTED_RESERVE,
            ).order_by(Registration.id.asc())
        )
    ).scalars().all()

    buttons = [[
        types.InlineKeyboardButton(
            text="❌ Исключить без замены",
            callback_data=f"reg_exclude_{reg_id}_{tour_id}",
        )
    ]]
    for reserve in reserves:
        buttons.append([types.InlineKeyboardButton(
            text=f"♻️ Заменить на {reserve.contact_telegram} ({reserve.game_nick})",
            callback_data=f"reg_promote_{reg_id}_{reserve.id}_{tour_id}",
        )])
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"tour_edit_members_{tour_id}",
    )])

    await callback.message.edit_text(
        f"⚙️ Действие для {kicked_reg.contact_telegram} ({kicked_reg.game_nick}):\n"
        "Выберите резервиста для замены или исключите без замены.",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@admin_router.callback_query(F.data.startswith("reg_exclude_"))
async def action_exclude_player(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    reg_id = int(parts[2])
    tour_id = int(parts[3])

    reg = (
        await db_session.execute(select(Registration).where(Registration.id == reg_id))
    ).scalar_one_or_none()
    if not reg:
        return

    reg.status = RegistrationStatus.EXCLUDED
    reg.exclusion_reason = "Исключен администратором"
    await db_session.commit()
    await menu_edit_tournament_members(
        callback,
        db_session,
        notice=f"❌ {reg.contact_telegram} исключён из состава.",
    )


@admin_router.callback_query(F.data.startswith("reg_promote_"))
async def action_promote_reserve(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.replacements import send_participation_request

    await callback.answer()
    parts = callback.data.split("_")
    old_id = int(parts[2])
    new_id = int(parts[3])
    tour_id = int(parts[4])

    old_reg = (
        await db_session.execute(select(Registration).where(Registration.id == old_id))
    ).scalar_one_or_none()
    new_reg = (
        await db_session.execute(select(Registration).where(Registration.id == new_id))
    ).scalar_one_or_none()
    if not old_reg or not new_reg:
        await _reply_or_edit(
            callback,
            "❌ Участник не найден.",
            types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"tour_edit_members_{tour_id}"),
            ]]),
        )
        return

    old_reg.status = RegistrationStatus.EXCLUDED
    old_reg.exclusion_reason = "Заменён резервистом"
    new_reg.status = RegistrationStatus.SELECTED_MAIN
    new_reg.participation_confirmed = False
    new_reg.participation_confirmed_at = None

    await db_session.commit()
    await send_participation_request(db_session, callback.bot, new_reg.id)
    await menu_edit_tournament_members(
        callback,
        db_session,
        notice=(
            f"♻️ {old_reg.contact_telegram} исключён.\n"
            f"📥 {new_reg.contact_telegram} включён в основной состав — "
            f"отправлен запрос на подтверждение."
        ),
    )


@admin_router.callback_query(F.data.startswith("reg_replace_unconfirmed_"))
async def action_replace_unconfirmed_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    main_players = await get_main_roster(db_session, tour_id)
    unconfirmed = [p for p in main_players if not p.participation_confirmed]
    reserves = (
        await db_session.execute(
            select(Registration).where(
                Registration.tournament_id == tour_id,
                Registration.status == RegistrationStatus.SELECTED_RESERVE,
            ).order_by(Registration.id.asc())
        )
    ).scalars().all()

    if not unconfirmed:
        await callback.message.answer("ℹ️ Все участники основного состава уже подтвердили участие.")
        return
    if not reserves:
        await callback.message.answer("❌ Нет доступных резервистов для замены.")
        return

    buttons = []
    for player in unconfirmed:
        for reserve in reserves:
            buttons.append([types.InlineKeyboardButton(
                text=f"{player.game_nick} → {reserve.game_nick}",
                callback_data=f"reg_promote_{player.id}_{reserve.id}_{tour_id}",
            )])
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"tour_edit_members_{tour_id}",
    )])
    await callback.message.edit_text(
        "♻️ Выберите пару: кого заменить и кем из резерва:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# --- РУЧНОЕ ДОБАВЛЕНИЕ УЧАСТНИКА ---
@admin_router.callback_query(F.data.startswith("admin_add_player_"))
async def admin_add_player_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    await state.update_data(manual_tour_id=tour_id)
    await state.set_state(AdminTournamentStates.waiting_for_manual_tg_id)
    await callback.message.answer(
        "➕ Ручное добавление участника.\n\n"
        "Введите Telegram ID или @username:"
    )


@admin_router.message(AdminTournamentStates.waiting_for_manual_tg_id)
async def admin_add_player_tg_id(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        return

    try:
        tg_id, username, contact = await resolve_telegram_contact(message.bot, message.text or "")
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    await state.update_data(manual_tg_id=tg_id, manual_username=username, manual_contact=contact)
    await state.set_state(AdminTournamentStates.waiting_for_manual_riot_id)
    await message.answer("Введите Riot ID в формате Имя#TAG:")


@admin_router.message(AdminTournamentStates.waiting_for_manual_riot_id)
async def admin_add_player_riot_id(message: types.Message, state: FSMContext):
    riot_id = message.text.strip()
    if "#" not in riot_id or len(riot_id) < 5:
        await message.answer("❌ Неверный формат Riot ID. Пример: Player#EUW")
        return
    await state.update_data(manual_riot_id=riot_id)
    await state.set_state(AdminTournamentStates.waiting_for_manual_rank)
    await message.answer("Введите ранг участника (например, Gold 2):")


@admin_router.message(AdminTournamentStates.waiting_for_manual_rank)
async def admin_add_player_rank(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
    role: str,
):
    from datetime import datetime

    rank = message.text.strip()
    if len(rank) < 2:
        await message.answer("❌ Укажите ранг текстом.")
        return

    data = await state.get_data()
    tour_id = data.get("manual_tour_id")
    tg_id = data.get("manual_tg_id")
    riot_id = data.get("manual_riot_id")
    if not tour_id or not tg_id or not riot_id:
        await state.clear()
        await message.answer("❌ Сессия добавления истекла. Начните заново.")
        return

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour or tour.status in (TournamentStatus.COMPLETED, TournamentStatus.CANCELLED):
        await state.clear()
        await message.answer("❌ Нельзя добавить участника в этот турнир.")
        return

    username = data.get("manual_username")
    contact = data.get("manual_contact") or f"ID {tg_id}"

    active_duplicate_statuses = (
        RegistrationStatus.REGISTERED,
        RegistrationStatus.SELECTED_MAIN,
        RegistrationStatus.SELECTED_RESERVE,
        RegistrationStatus.EXCLUDED,
        RegistrationStatus.NOT_SELECTED,
        RegistrationStatus.WITHDRAWN,
    )

    async def _get_or_create_user() -> User:
        db_user = (
            await db_session.execute(select(User).where(User.telegram_id == tg_id))
        ).scalar_one_or_none()
        if not db_user:
            db_user = User(telegram_id=tg_id, telegram_username=username)
            db_session.add(db_user)
            await db_session.flush()
        else:
            db_user.telegram_username = username
        return db_user

    async def _ensure_no_active_registration(db_user: User) -> bool:
        duplicate = (
            await db_session.execute(
                select(Registration).where(
                    Registration.tournament_id == tour_id,
                    Registration.user_id == db_user.id,
                    Registration.status.in_(active_duplicate_statuses),
                )
            )
        ).scalar_one_or_none()
        if duplicate:
            await state.clear()
            await message.answer("ℹ️ У этого участника уже есть заявка на турнир.")
            return False
        return True

    def _new_outside_match_registration(user_id: int) -> Registration:
        return Registration(
            tournament_id=tour_id,
            user_id=user_id,
            game_nick=riot_id,
            game_rank=rank,
            contact_telegram=contact,
            status=RegistrationStatus.EXCLUDED,
            exclusion_reason=OUTSIDE_MATCH_REASON,
            excluded_at=now_moscow(),
            subscription_status=SubscriptionStatus.SUBSCRIBED,
            rules_accepted=True,
            rules_accepted_at=now_moscow(),
            participation_confirmed=False,
            participation_confirmed_at=None,
        )

    manual_flow = data.get("manual_flow")

    if manual_flow in ("group_replace", "stage_replace"):
        group_id = data.get("manual_replace_group_id")
        old_reg_id = data.get("manual_replace_old_reg_id")
        if not group_id or not old_reg_id:
            await state.clear()
            await message.answer("❌ Сессия замены истекла. Начните заново.")
            return

        db_user = await _get_or_create_user()
        if not await _ensure_no_active_registration(db_user):
            return

        new_reg = _new_outside_match_registration(db_user.id)
        db_session.add(new_reg)
        await db_session.flush()

        admin = (
            await db_session.execute(select(Admin).where(Admin.telegram_id == message.from_user.id))
        ).scalar_one_or_none()
        try:
            replaced_reg = await replace_group_member(
                db_session,
                message.bot,
                tour_id,
                group_id,
                old_reg_id,
                new_reg.id,
                admin_id=admin.id if admin else None,
                send_notifications=True,
            )
            await db_session.commit()
            await state.clear()
            if manual_flow == "stage_replace":
                manual_stage_team_id = data.get("manual_stage_team_id")
                manual_stage_team_label = data.get("manual_stage_team_label")
                if manual_stage_team_id and manual_stage_team_label:
                    await message.answer(
                        f"✅ {contact} добавлен в список «Вне матча» и поставлен на замену.",
                        reply_markup=types.InlineKeyboardMarkup(
                            inline_keyboard=[[
                                types.InlineKeyboardButton(
                                    text=f"✏️ Вернуться к команде {manual_stage_team_label}",
                                    callback_data=(
                                        f"stage_team_view_{manual_stage_team_id}_"
                                        f"{manual_stage_team_label}"
                                    ),
                                )
                            ]]
                        ),
                    )
                else:
                    from bot.handlers.stages import _get_active_stage_for_group

                    active_stage = await _get_active_stage_for_group(db_session, group_id)
                    await message.answer(
                        f"✅ {contact} добавлен в список «Вне матча» и поставлен на замену.",
                        reply_markup=replacement_followup_keyboard(
                            tour_id,
                            group_id,
                            replaced_reg.id,
                            stage_id=active_stage.id if active_stage else None,
                            show_dev_confirm=(
                                role == "developer"
                                or message.from_user.id == settings.DEVELOPER_TG_ID
                            ),
                        ),
                    )
            else:
                await message.answer(
                    f"✅ {contact} добавлен и заменил участника в группе.",
                    reply_markup=types.InlineKeyboardMarkup(
                        inline_keyboard=[[
                            types.InlineKeyboardButton(
                                text="✏️ Вернуться к группе",
                                callback_data=f"tour_edit_group_{tour_id}_{group_id}",
                            )
                        ]]
                    ),
                )
        except ValueError as exc:
            await db_session.rollback()
            await state.clear()
            await message.answer(f"❌ {exc}")
        return

    if manual_flow == "stage_team_add":
        stage_id = data.get("manual_stage_id")
        team_label = data.get("manual_stage_team_label")
        if not stage_id or not team_label:
            await state.clear()
            await message.answer("❌ Сессия добавления истекла. Начните заново.")
            return

        db_user = await _get_or_create_user()
        if not await _ensure_no_active_registration(db_user):
            return

        new_reg = _new_outside_match_registration(db_user.id)
        db_session.add(new_reg)
        await db_session.flush()

        admin = (
            await db_session.execute(select(Admin).where(Admin.telegram_id == message.from_user.id))
        ).scalar_one_or_none()
        from bot.db.models import TeamLabel
        from bot.services.stages import assign_player_to_stage_team

        try:
            await assign_player_to_stage_team(
                db_session,
                message.bot,
                stage_id,
                new_reg.id,
                TeamLabel(team_label),
                admin_id=admin.id if admin else None,
                send_notifications=True,
            )
            await db_session.commit()
            await state.clear()
            await message.answer(
                f"✅ {contact} добавлен в команду {team_label}.",
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[[
                        types.InlineKeyboardButton(
                            text=f"✏️ Вернуться к команде {team_label}",
                            callback_data=f"stage_team_view_{stage_id}_{team_label}",
                        )
                    ]]
                ),
            )
        except ValueError as exc:
            await db_session.rollback()
            await state.clear()
            await message.answer(f"❌ {exc}")
        return

    if manual_flow == "group_add":
        group_id = data.get("manual_add_group_id")
        if not group_id:
            await state.clear()
            await message.answer("❌ Сессия добавления истекла. Начните заново.")
            return

        db_user = await _get_or_create_user()
        if not await _ensure_no_active_registration(db_user):
            return

        new_reg = _new_outside_match_registration(db_user.id)
        db_session.add(new_reg)
        await db_session.flush()

        admin = (
            await db_session.execute(select(Admin).where(Admin.telegram_id == message.from_user.id))
        ).scalar_one_or_none()
        try:
            await add_member_to_group(
                db_session,
                message.bot,
                tour_id,
                group_id,
                new_reg.id,
                admin_id=admin.id if admin else None,
                send_notifications=True,
            )
            await db_session.commit()
            await state.clear()
            await message.answer(
                f"✅ {contact} добавлен в группу. Отправлен запрос на подтверждение участия.",
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[[
                        types.InlineKeyboardButton(
                            text="✏️ Вернуться к группе",
                            callback_data=f"tour_edit_group_{tour_id}_{group_id}",
                        )
                    ]]
                ),
            )
        except ValueError as exc:
            await db_session.rollback()
            await state.clear()
            await message.answer(f"❌ {exc}")
        return

    db_user = await _get_or_create_user()
    if not await _ensure_no_active_registration(db_user):
        return

    reg = _new_outside_match_registration(db_user.id)
    db_session.add(reg)
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ Участник {contact} добавлен в список «Вне матча»."
    )


@admin_router.callback_query(F.data.startswith("admin_add_player_list_"))
async def admin_add_player_list_choice(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    from datetime import datetime

    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[4])
    tg_id = int(parts[5])
    list_choice = parts[6]

    data = await state.get_data()
    riot_id = data.get("manual_riot_id")
    rank = data.get("manual_rank")
    contact = data.get("manual_contact")
    if not riot_id or not rank:
        await callback.message.edit_text("❌ Сессия добавления истекла. Начните заново.")
        await state.clear()
        return

    status_map = {
        "main": RegistrationStatus.SELECTED_MAIN,
        "reserve": RegistrationStatus.SELECTED_RESERVE,
        "queue": RegistrationStatus.REGISTERED,
    }
    status = status_map.get(list_choice)
    if not status:
        await callback.message.edit_text("❌ Некорректный выбор.")
        return

    db_user = (
        await db_session.execute(select(User).where(User.telegram_id == tg_id))
    ).scalar_one_or_none()
    if not db_user:
        await callback.message.edit_text("❌ Пользователь не найден. Начните добавление заново.")
        await state.clear()
        return

    reg = Registration(
        tournament_id=tour_id,
        user_id=db_user.id,
        game_nick=riot_id,
        game_rank=rank,
        contact_telegram=contact or f"ID {tg_id}",
        status=status,
        subscription_status=SubscriptionStatus.SUBSCRIBED,
        rules_accepted=True,
        rules_accepted_at=now_moscow(),
        participation_confirmed=False if status == RegistrationStatus.SELECTED_MAIN else True,
    )
    db_session.add(reg)
    await db_session.flush()
    if status == RegistrationStatus.SELECTED_MAIN:
        from bot.services.replacements import send_participation_request
        await send_participation_request(db_session, callback.bot, reg.id)
    await db_session.commit()
    await state.clear()

    labels = {
        RegistrationStatus.SELECTED_MAIN: "основной состав",
        RegistrationStatus.SELECTED_RESERVE: "резерв",
        RegistrationStatus.REGISTERED: "очередь",
    }
    await callback.message.edit_text(
        f"✅ Участник добавлен в {labels[status]}."
    )


# --- ФОРМИРОВАНИЕ ГРУПП ПО 10 ЧЕЛОВЕК (ТЗ) ---
@admin_router.callback_query(F.data.startswith("tour_build_groups_"))
async def action_build_random_groups(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    await callback.answer()
    tour_id = int(callback.data.split("_")[3])

    tour = (await db_session.execute(
        select(Tournament).where(Tournament.id == tour_id)
    )).scalar_one_or_none()
    if not tour:
        await callback.message.answer("❌ Турнир не найден.")
        return

    if tour.status != TournamentStatus.CONFIRMATION_PENDING:
        await callback.message.answer("❌ Формирование групп доступно только на этапе ожидания подтверждений.")
        return

    if await tournament_has_groups(db_session, tour_id):
        await callback.message.answer("ℹ️ Группы уже сформированы. Откройте «Списки групп» для просмотра.")
        return

    main_players = await get_main_roster(db_session, tour_id)
    if not main_players:
        await callback.message.answer("❌ Основной состав пуст. Сначала проведите отбор участников.")
        return

    unconfirmed = get_unconfirmed_players(main_players)
    if unconfirmed:
        names = ", ".join(f"{player.contact_telegram} ({player.game_nick})" for player in unconfirmed[:10])
        suffix = "..." if len(unconfirmed) > 10 else ""
        await callback.message.answer(
            "❌ Нельзя сформировать группы: не все участники основного состава подтвердили участие.\n"
            f"Ожидают подтверждения ({len(unconfirmed)}): {names}{suffix}"
        )
        return

    group_size = resolve_group_capacity(tour)
    if len(main_players) % group_size != 0:
        await callback.message.answer(
            f"❌ Число участников основного состава ({len(main_players)} чел.) "
            f"должно быть кратно {group_size}."
        )
        return

    rng = secrets.SystemRandom()
    shuffled = list(main_players)
    rng.shuffle(shuffled)

    group_number = 1
    for chunk_start in range(0, len(shuffled), group_size):
        chunk = shuffled[chunk_start:chunk_start + group_size]

        new_group = TournamentGroup(tournament_id=tour_id, group_number=group_number)
        db_session.add(new_group)
        await db_session.flush()

        for member in chunk:
            db_session.add(GroupMember(group_id=new_group.id, registration_id=member.id))

        group_number += 1

    tour.status = TournamentStatus.GROUPS_FORMED
    await ensure_group_stage_started(db_session, tour_id)
    await db_session.commit()

    groups_text = await format_tournament_groups_text(db_session, tour_id, admin_view=True)
    await callback.message.answer(
        f"🎉 Сформировано групп: {group_number - 1} (по {group_size} участников случайным образом).\n\n{groups_text}",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[
                types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"manage_tour_{tour_id}"),
            ]]
        ),
    )
    await manage_single_tournament(callback, db_session, role)


@admin_router.callback_query(F.data.startswith("tour_view_groups_"))
async def view_tournament_groups(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[3])

    groups_text = await format_tournament_groups_text(db_session, tour_id, admin_view=True)
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    buttons = []
    if tour and tour.status in (TournamentStatus.GROUPS_FORMED, TournamentStatus.STAGE_IN_PROGRESS):
        buttons.append([types.InlineKeyboardButton(
            text="✏️ Редактировать состав групп",
            callback_data=f"tour_edit_groups_{tour_id}",
        )])
    buttons.append([types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"manage_tour_{tour_id}")])
    await _reply_or_edit(
        callback,
        groups_text,
        types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@admin_router.callback_query(
    F.data.startswith("tour_stages_") | F.data.startswith("stage_dash_")
)
async def admin_open_stages(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    from bot.handlers.stages import _open_stage_dashboard

    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    await _open_stage_dashboard(callback, db_session, tour_id, role)


@admin_router.callback_query(F.data.regexp(r"^tour_edit_groups_\d+$"))
async def tour_edit_groups_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[3])
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    group_size = resolve_group_capacity(tour)
    groups = await get_tournament_groups(db_session, tour_id)
    if not groups:
        await _reply_or_edit(
            callback,
            "❌ Группы ещё не сформированы.",
            types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"manage_tour_{tour_id}"),
            ]]),
        )
        return

    buttons = []
    for group in groups:
        members = await get_group_members(db_session, group.id)
        member_count = len(members)
        buttons.append([types.InlineKeyboardButton(
            text=f"Группа {group.group_number} ({member_count}/{group_size})",
            callback_data=f"tour_edit_group_{tour_id}_{group.id}",
        )])
        if member_count < group_size:
            buttons.append([types.InlineKeyboardButton(
                text=f"➕ Добавить в группу {group.group_number}",
                callback_data=f"tour_group_addmenu_{tour_id}_{group.id}",
            )])
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"tour_view_groups_{tour_id}",
    )])
    await callback.message.edit_text(
        "✏️ Выберите группу для редактирования состава:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@admin_router.callback_query(F.data.regexp(r"^tour_edit_group_\d+_\d+$"))
async def tour_edit_single_group(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    group_id = int(parts[4])

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    group_size = resolve_group_capacity(tour)
    members = await get_group_members(db_session, group_id)
    member_count = len(members)

    text = await format_single_group_text(db_session, tour_id, group_id, admin_view=True)
    buttons = []
    for member in members:
        buttons.append([types.InlineKeyboardButton(
            text=f"👤 {member.game_nick} ({member.contact_telegram})",
            callback_data=f"tour_group_pick_{tour_id}_{group_id}_{member.id}",
        )])
    if member_count < group_size:
        buttons.append([types.InlineKeyboardButton(
            text=f"➕ Добавить участника ({member_count}/{group_size})",
            callback_data=f"tour_group_addmenu_{tour_id}_{group_id}",
        )])
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"tour_edit_groups_{tour_id}",
    )])
    action_hint = (
        f"В группе {member_count} из {group_size}. Нажмите «➕ Добавить участника», чтобы заполнить состав."
        if member_count < group_size
        else "Выберите участника для редактирования:"
    )
    await callback.message.edit_text(
        f"{text}\n\n{action_hint}",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@admin_router.callback_query(F.data.startswith("tour_group_pick_"))
async def tour_group_member_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    group_id = int(parts[4])
    reg_id = int(parts[5])

    reg = (
        await db_session.execute(select(Registration).where(Registration.id == reg_id))
    ).scalar_one_or_none()
    if not reg:
        await callback.message.edit_text("❌ Участник не найден.")
        return

    groups = await get_tournament_groups(db_session, tour_id)
    group = next((g for g in groups if g.id == group_id), None)
    group_label = f"группы {group.group_number}" if group else "группы"

    buttons = [
        [types.InlineKeyboardButton(
            text="♻️ Заменить",
            callback_data=f"tour_group_replmenu_{tour_id}_{group_id}_{reg_id}",
        )],
        [types.InlineKeyboardButton(
            text="❌ Удалить из группы",
            callback_data=f"tour_group_rem_ask_{tour_id}_{group_id}_{reg_id}",
        )],
    ]
    other_groups = [g for g in groups if g.id != group_id]
    if other_groups:
        buttons.append([types.InlineKeyboardButton(
            text="➡️ Перенести в другую группу",
            callback_data=f"tour_group_xfer_menu_{tour_id}_{group_id}_{reg_id}",
        )])
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"tour_edit_group_{tour_id}_{group_id}",
    )])
    await callback.message.edit_text(
        f"👤 {reg.contact_telegram} ({reg.game_nick})\n"
        f"Группа: {group_label}\n\nВыберите действие:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@admin_router.callback_query(F.data.startswith("tour_group_rem_ok_"))
async def tour_group_remove_confirm(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[4])
    group_id = int(parts[5])
    reg_id = int(parts[6])

    try:
        reg = await remove_group_member(db_session, tour_id, group_id, reg_id)
        await db_session.commit()
        await callback.message.edit_text(
            f"✅ {reg.contact_telegram} удалён из группы.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="✏️ Вернуться к группе",
                        callback_data=f"tour_edit_group_{tour_id}_{group_id}",
                    )
                ]]
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@admin_router.callback_query(F.data.startswith("tour_group_rem_ask_"))
async def tour_group_remove_prompt(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[4])
    group_id = int(parts[5])
    reg_id = int(parts[6])

    reg = (
        await db_session.execute(select(Registration).where(Registration.id == reg_id))
    ).scalar_one_or_none()
    if not reg:
        await callback.message.edit_text("❌ Участник не найден.")
        return

    await callback.message.edit_text(
        f"❌ Удалить {reg.contact_telegram} ({reg.game_nick}) из группы?\n\n"
        "Участник попадёт в список «Вне матча». Замену нужно будет сделать отдельно.",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="✅ Да, удалить",
                        callback_data=f"tour_group_rem_ok_{tour_id}_{group_id}_{reg_id}",
                    ),
                    types.InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"tour_group_pick_{tour_id}_{group_id}_{reg_id}",
                    ),
                ]
            ]
        ),
    )


@admin_router.callback_query(F.data.startswith("tour_group_replace_do_"))
async def tour_group_replace_reserve(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[4])
    group_id = int(parts[5])
    old_reg_id = int(parts[6])
    new_reg_id = int(parts[7])

    admin = (
        await db_session.execute(select(Admin).where(Admin.telegram_id == callback.from_user.id))
    ).scalar_one_or_none()
    try:
        new_reg = await replace_group_member(
            db_session,
            callback.bot,
            tour_id,
            group_id,
            old_reg_id,
            new_reg_id,
            admin_id=admin.id if admin else None,
        )
        await db_session.commit()
        await callback.message.edit_text(
            f"✅ Замена выполнена: {new_reg.contact_telegram} ({new_reg.game_nick}) "
            "включён в группу. Отправлен запрос на подтверждение участия.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="✏️ Вернуться к группе",
                        callback_data=f"tour_edit_group_{tour_id}_{group_id}",
                    )
                ]]
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@admin_router.callback_query(F.data.startswith("tour_group_replace_new_"))
async def tour_group_replace_new_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[4])
    group_id = int(parts[5])
    old_reg_id = int(parts[6])

    await state.update_data(
        manual_tour_id=tour_id,
        manual_flow="group_replace",
        manual_replace_group_id=group_id,
        manual_replace_old_reg_id=old_reg_id,
    )
    await state.set_state(AdminTournamentStates.waiting_for_manual_tg_id)
    await callback.message.edit_text(
        "➕ Создание нового участника для замены.\n\n"
        "Введите Telegram ID или @username:"
    )


@admin_router.callback_query(F.data.startswith("tour_group_replmenu_"))
async def tour_group_replace_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    group_id = int(parts[4])
    reg_id = int(parts[5])

    reg = (
        await db_session.execute(select(Registration).where(Registration.id == reg_id))
    ).scalar_one_or_none()
    if not reg:
        await callback.message.edit_text("❌ Участник не найден.")
        return

    reserves = await get_available_reserves(db_session, tour_id)
    buttons = []
    for reserve in reserves:
        buttons.append([types.InlineKeyboardButton(
            text=f"🔄 Резерв: {reserve.contact_telegram} ({reserve.game_nick})",
            callback_data=f"tour_group_replace_do_{tour_id}_{group_id}_{reg_id}_{reserve.id}",
        )])
    buttons.append([types.InlineKeyboardButton(
        text="➕ Создать нового участника",
        callback_data=f"tour_group_replace_new_{tour_id}_{group_id}_{reg_id}",
    )])
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"tour_group_pick_{tour_id}_{group_id}_{reg_id}",
    )])

    text = f"♻️ Замена для {reg.contact_telegram} ({reg.game_nick}):\n"
    if not reserves:
        text += "\nРезерв пуст — можно только создать нового участника."
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@admin_router.callback_query(F.data.startswith("tour_group_addmenu_"))
async def tour_group_add_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    group_id = int(parts[4])

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    group_size = resolve_group_capacity(tour)
    members = await get_group_members(db_session, group_id)
    member_count = len(members)
    if member_count >= group_size:
        await callback.message.edit_text(
            f"❌ В группе уже {group_size} участников. Сначала удалите кого-то или замените.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=f"tour_edit_group_{tour_id}_{group_id}",
                    )
                ]]
            ),
        )
        return

    reserves = await get_available_reserves(db_session, tour_id)
    outside = await get_outside_roster_candidates(db_session, tour_id)
    buttons = []
    for reserve in reserves:
        buttons.append([types.InlineKeyboardButton(
            text=f"🔄 Резерв: {reserve.contact_telegram} ({reserve.game_nick})",
            callback_data=f"tour_group_add_do_{tour_id}_{group_id}_{reserve.id}",
        )])
    for candidate in outside:
        buttons.append([types.InlineKeyboardButton(
            text=f"⚪ Вне матча: {candidate.contact_telegram} ({candidate.game_nick})",
            callback_data=f"tour_group_add_do_{tour_id}_{group_id}_{candidate.id}",
        )])
    buttons.append([types.InlineKeyboardButton(
        text="➕ Создать нового участника",
        callback_data=f"tour_group_add_new_{tour_id}_{group_id}",
    )])
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"tour_edit_group_{tour_id}_{group_id}",
    )])

    text = "➕ Добавление участника в группу:\n"
    text += f"Сейчас в группе {member_count} из {group_size}.\n"
    if not reserves and not outside:
        text += "\nРезерв и список «Вне матча» пусты — можно только создать нового участника."
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@admin_router.callback_query(F.data.startswith("tour_group_add_do_"))
async def tour_group_add_reserve(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[4])
    group_id = int(parts[5])
    reg_id = int(parts[6])

    admin = (
        await db_session.execute(select(Admin).where(Admin.telegram_id == callback.from_user.id))
    ).scalar_one_or_none()
    try:
        new_reg = await add_member_to_group(
            db_session,
            callback.bot,
            tour_id,
            group_id,
            reg_id,
            admin_id=admin.id if admin else None,
            send_notifications=True,
        )
        await db_session.commit()
        await callback.message.edit_text(
            f"✅ {new_reg.contact_telegram} ({new_reg.game_nick}) добавлен в группу. "
            "Отправлен запрос на подтверждение участия.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="✏️ Вернуться к группе",
                        callback_data=f"tour_edit_group_{tour_id}_{group_id}",
                    )
                ]]
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@admin_router.callback_query(F.data.startswith("tour_group_add_new_"))
async def tour_group_add_new_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[4])
    group_id = int(parts[5])

    await state.update_data(
        manual_tour_id=tour_id,
        manual_flow="group_add",
        manual_add_group_id=group_id,
    )
    await state.set_state(AdminTournamentStates.waiting_for_manual_tg_id)
    await callback.message.edit_text(
        "➕ Создание нового участника для группы.\n\n"
        "Введите Telegram ID или @username:"
    )


@admin_router.callback_query(F.data.startswith("tour_group_xfer_menu_"))
async def tour_group_xfer_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[4])
    group_id = int(parts[5])
    reg_id = int(parts[6])

    reg = (
        await db_session.execute(select(Registration).where(Registration.id == reg_id))
    ).scalar_one_or_none()
    if not reg:
        await callback.message.edit_text("❌ Участник не найден.")
        return

    groups = await get_tournament_groups(db_session, tour_id)
    other_groups = [g for g in groups if g.id != group_id]
    buttons = [
        [types.InlineKeyboardButton(
            text=f"➡️ Группа {target.group_number}",
            callback_data=f"tour_group_xfer_{tour_id}_{reg_id}_{target.id}",
        )]
        for target in other_groups
    ]
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"tour_group_pick_{tour_id}_{group_id}_{reg_id}",
    )])
    await callback.message.edit_text(
        f"➡️ Перенос {reg.game_nick} в другую группу:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@admin_router.callback_query(F.data.startswith("tour_group_xfer_"))
async def tour_group_xfer_member(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    reg_id = int(parts[4])
    target_group_id = int(parts[5])

    try:
        await move_member_between_groups(db_session, tour_id, reg_id, target_group_id)
        await db_session.commit()
        groups_text = await format_tournament_groups_text(db_session, tour_id, admin_view=True)
        await callback.message.answer(
            f"✅ Участник перенесён.\n\n{groups_text}",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="✏️ Редактировать группы",
                        callback_data=f"tour_edit_groups_{tour_id}",
                    )
                ]]
            ),
        )
    except ValueError as exc:
        await callback.message.answer(f"❌ {exc}")


# --- СЦЕНАРИЙ ОТБОРА УЧАСТНИКОВ (FSM) ---
@admin_router.callback_query(F.data.startswith("start_manual_select_"))
async def action_start_manual_selection(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    tour_id = int(callback.data.split("_")[3])
    await state.update_data(select_tour_id=tour_id)
    
    await callback.message.answer("🎲 Шаг 1: Введите количество игроков основного состава (например, 15 или 20):")
    await state.set_state(SelectionStates.waiting_for_main_count)

@admin_router.message(SelectionStates.waiting_for_main_count)
async def process_main_selection_count(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("❌ Пожалуйста, введите число:")
        return

    main_count = int(text)
    if main_count <= 0 or main_count % 10 != 0:
        await message.answer(
            "❌ Число участников основного состава (N) должно быть положительным и кратным 10."
        )
        return

    await state.update_data(main_count=main_count)
    await message.answer("🎲 Шаг 2: Введите количество мест в резерве (например, 5):")
    await state.set_state(SelectionStates.waiting_for_reserve_count)

@admin_router.message(SelectionStates.waiting_for_reserve_count)
async def process_reserve_selection_count(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("❌ Пожалуйста, введите число:")
        return

    reserve_count = int(text)
    if reserve_count < 0:
        await message.answer("❌ Количество резервных мест не может быть отрицательным.")
        return

    data = await state.get_data()
    tour_id = data.get("select_tour_id")
    main_count = data.get("main_count")

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        await message.answer("❌ Турнир не найден.")
        await state.clear()
        return

    try:
        draft = await build_random_draft(
            db_session, message.bot, tour, main_count, reserve_count
        )
        await save_selection_draft(db_session, tour_id, draft)
        await db_session.commit()
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        await state.clear()
        return

    await state.clear()
    draft_text = await format_draft_text(db_session, draft)
    await message.answer(
        draft_text,
        reply_markup=draft_preview_keyboard(tour_id),
    )


async def _show_selection_draft(callback: types.CallbackQuery, db_session: AsyncSession, tour_id: int):
    draft = await get_selection_draft(db_session, tour_id)
    if not draft:
        await callback.message.answer("❌ Черновик отбора не найден. Запустите отбор заново.")
        return
    draft_text = await format_draft_text(db_session, draft)
    await callback.message.edit_text(
        draft_text,
        reply_markup=draft_preview_keyboard(tour_id),
    )


@admin_router.callback_query(F.data.startswith("sel_reroll_"))
async def selection_reroll(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    draft = await get_selection_draft(db_session, tour_id)
    if not draft:
        await callback.message.answer("❌ Черновик отбора не найден.")
        return

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        return

    try:
        new_draft = await build_random_draft(
            db_session,
            callback.bot,
            tour,
            draft["main_count"],
            draft["reserve_count"],
        )
        await save_selection_draft(db_session, tour_id, new_draft)
        await db_session.commit()
        draft = new_draft
    except ValueError as exc:
        await callback.message.answer(f"❌ {exc}")
        return

    await _show_selection_draft(callback, db_session, tour_id)


@admin_router.callback_query(F.data.startswith("sel_confirm_"))
async def selection_confirm(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    draft = await get_selection_draft(db_session, tour_id)
    if not draft:
        await callback.message.answer("❌ Черновик отбора не найден.")
        return

    try:
        await apply_selection_draft(db_session, callback.bot, tour_id, draft)
        await db_session.commit()
        await callback.message.answer(
            "✅ Отбор подтверждён. Участникам отправлены уведомления о результате.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="⬅️ К управлению турниром",
                        callback_data=f"manage_tour_{tour_id}",
                    )
                ]]
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.answer(f"❌ {exc}")


def _selection_move_keyboard(tour_id: int, reg_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="🟢 В основной",
                    callback_data=f"sel_move_{tour_id}_{reg_id}_main",
                ),
                types.InlineKeyboardButton(
                    text="🔵 В резерв",
                    callback_data=f"sel_move_{tour_id}_{reg_id}_reserve",
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text="⚪ Не прошли",
                    callback_data=f"sel_move_{tour_id}_{reg_id}_not_selected",
                )
            ],
            [types.InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"sel_back_{tour_id}",
            )],
        ]
    )


async def _selection_edit_list(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    tour_id: int,
    list_name: str,
    title: str,
):
    draft = await get_selection_draft(db_session, tour_id)
    if not draft:
        await callback.message.answer("❌ Черновик отбора не найден.")
        return

    ids_key = f"{list_name}_ids" if list_name != "not_selected" else "not_selected_ids"
    reg_ids = draft.get(ids_key, [])
    if not reg_ids:
        await callback.message.answer(f"Список «{title}» пуст.")
        return

    rows = (
        await db_session.execute(select(Registration).where(Registration.id.in_(reg_ids)))
    ).scalars().all()
    by_id = {row.id: row for row in rows}

    buttons = []
    for reg_id in reg_ids:
        reg = by_id.get(reg_id)
        if not reg:
            continue
        buttons.append([types.InlineKeyboardButton(
            text=f"✏️ {reg.contact_telegram} ({reg.game_nick})",
            callback_data=f"sel_pick_{tour_id}_{reg_id}",
        )])
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"sel_back_{tour_id}",
    )])

    await callback.message.edit_text(
        f"✏️ Редактирование: {title}\nВыберите участника для переноса в другой список:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@admin_router.callback_query(F.data.startswith("sel_edit_main_"))
async def selection_edit_main(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    await _selection_edit_list(callback, db_session, tour_id, "main", "основной состав")


@admin_router.callback_query(F.data.startswith("sel_edit_reserve_"))
async def selection_edit_reserve(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    await _selection_edit_list(callback, db_session, tour_id, "reserve", "резерв")


@admin_router.callback_query(F.data.startswith("sel_pick_"))
async def selection_pick_player(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[2])
    reg_id = int(parts[3])
    await callback.message.edit_text(
        "Куда перенести участника?",
        reply_markup=_selection_move_keyboard(tour_id, reg_id),
    )


@admin_router.callback_query(F.data.startswith("sel_move_"))
async def selection_move_player(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[2])
    reg_id = int(parts[3])
    target = "_".join(parts[4:])

    try:
        await swap_registration_in_draft(db_session, tour_id, reg_id, target)
        await db_session.commit()
    except ValueError as exc:
        await callback.message.answer(f"❌ {exc}")
        return

    await _show_selection_draft(callback, db_session, tour_id)


@admin_router.callback_query(F.data.startswith("sel_back_"))
async def selection_back_to_draft(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    await _show_selection_draft(callback, db_session, tour_id)


# --- АВТОПОДТВЕРЖДЕНИЕ БОТОВ РАЗРАБОТЧИКОМ ---
@admin_router.callback_query(F.data.startswith("test_confirm_all_"))
async def action_test_confirm_all_bots(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    if not await _deny_unless_developer(callback, role):
        return
    await callback.answer()
    tour_id = int(callback.data.split("_")[3])

    try:
        query = select(Registration).where(
            Registration.tournament_id == tour_id,
            Registration.status == RegistrationStatus.SELECTED_MAIN
        )
        main_players = (await db_session.execute(query)).scalars().all()

        was_all_confirmed = await all_main_roster_confirmed(db_session, tour_id)
        counter = 0
        for player in main_players:
            if not player.participation_confirmed:
                player.participation_confirmed = True
                counter += 1

        await db_session.commit()
        if not was_all_confirmed and await all_main_roster_confirmed(db_session, tour_id):
            await notify_admins_all_main_roster_confirmed(callback.bot, db_session, tour_id)
        await callback.message.answer(f"🧪 Успешно подтверждено участие для {counter} игроков!")
        await manage_single_tournament(callback, db_session, role)
        
    except Exception as e:
        await db_session.rollback()
        await callback.message.answer(f"❌ Ошибка автоподтверждения: {e}")


@admin_router.callback_query(F.data.startswith("test_confirm_finalists_"))
async def action_test_confirm_finalists(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    from bot.services.final_stage import (
        all_finalists_confirmed,
        confirm_all_finalists_dev,
        notify_admins_all_finalists_confirmed,
    )

    if not await _deny_unless_developer(callback, role):
        return
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    try:
        was_all_confirmed = await all_finalists_confirmed(db_session, tour_id)
        counter = await confirm_all_finalists_dev(db_session, tour_id)
        await db_session.commit()
        if not was_all_confirmed and await all_finalists_confirmed(db_session, tour_id):
            await notify_admins_all_finalists_confirmed(callback.bot, db_session, tour_id)
        await callback.message.answer(f"🧪 Подтверждено финалистов: {counter}")
        await manage_single_tournament(callback, db_session, role)
    except Exception as e:
        await db_session.rollback()
        await callback.message.answer(f"❌ Ошибка: {e}")


# --- ПОЛНОЕ УДАЛЕНИЕ ТУРНИРА ---
@admin_router.callback_query(F.data.startswith("tour_delete_confirm_"))
async def action_delete_tournament_prompt(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[3])

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        await callback.message.answer("❌ Турнир не найден.")
        return

    await callback.message.edit_text(
        f"⚠️ Удалить турнир «{tour.title}»?\n"
        "Все заявки, группы, матчи и результаты будут удалены без возможности восстановления.",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=f"tour_delete_force_{tour_id}",
                )],
                [types.InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"manage_tour_{tour_id}",
                )],
            ]
        ),
    )


@admin_router.callback_query(F.data.startswith("tour_delete_force_"))
async def action_delete_tournament_completely(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    
    try:
        await delete_tournament_cascade(db_session, tour_id)
        await db_session.commit()
        await callback.message.answer("🗑 Турнир и все связанные данные полностью удалены.")
    except Exception as e:
        await db_session.rollback()
        await callback.message.answer(f"❌ Ошибка удаления: {e}")
        
    await view_tournaments_list(callback, db_session)


# --- ПРОСМОТР СПИСКОВ УЧАСТНИКОВ ---
@admin_router.callback_query(F.data.startswith("tour_lists_"))
async def view_tournament_lists(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[2])

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()

    query = select(Registration).where(Registration.tournament_id == tour_id)
    res = await db_session.execute(query)
    registrations = res.scalars().all()

    if not registrations:
        await _reply_or_edit(
            callback,
            "📋 На этот турнир пока никто не зарегистрировался.",
            types.InlineKeyboardMarkup(inline_keyboard=[[
                types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"manage_tour_{tour_id}")
            ]]),
        )
        return

    points_map = None
    if tour and tour.status in (
        TournamentStatus.GROUPS_FORMED,
        TournamentStatus.STAGE_IN_PROGRESS,
        TournamentStatus.RATING_CALCULATED,
        TournamentStatus.FINALISTS_SELECTED,
        TournamentStatus.FINAL_IN_PROGRESS,
        TournamentStatus.COMPLETED,
    ):
        from bot.services.scoring import aggregate_player_points

        points_map = {
            reg.id: points
            for reg, points in await aggregate_player_points(
                db_session, tour_id, group_stage_only=True
            )
        }

    text = format_participant_lists_text(registrations, tour_id, points_by_reg_id=points_map)
    buttons = [[types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"manage_tour_{tour_id}")]]
    await _reply_or_edit(callback, text, types.InlineKeyboardMarkup(inline_keyboard=buttons))


# --- ИНСТРУМЕНТЫ РАЗРАБОТЧИКА (УПРАВЛЕНИЕ АДМИНАМИ) ---
@admin_router.callback_query(F.data == "dev_manage_admins")
async def dev_manage_admins_menu(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    if not await _deny_unless_developer(callback, role):
        return
    await callback.answer()

    query = select(Admin).where(Admin.role == AdminRole.ADMIN)
    res = await db_session.execute(query)
    admins = res.scalars().all()

    text = "👥 Управление администраторами проекта:\n\nНажмите на администратора для его удаления из системы."
    buttons = []

    for a in admins:
        buttons.append([types.InlineKeyboardButton(
            text=f"❌ Удалить ID: {a.telegram_id}", callback_data=f"dev_del_admin_{a.id}"
        )])

    buttons.append([types.InlineKeyboardButton(text="➕ Добавить администратора", callback_data="dev_add_admin_start")])
    buttons.append([types.InlineKeyboardButton(text="⬅️ В главное меню", callback_data="admin_home")])
    await _reply_or_edit(callback, text, types.InlineKeyboardMarkup(inline_keyboard=buttons))

@admin_router.callback_query(F.data == "dev_add_admin_start")
async def dev_add_admin_start(callback: types.CallbackQuery, state: FSMContext, role: str):
    if not await _deny_unless_developer(callback, role):
        return
    await callback.answer()
    await callback.message.answer("Введите Telegram ID нового администратора цифрами:")
    await state.set_state(DeveloperStates.waiting_for_new_admin_id)

@admin_router.message(DeveloperStates.waiting_for_new_admin_id)
async def dev_process_add_admin(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
    role: str,
):
    if not await _deny_unless_developer(message, role):
        await state.clear()
        return

    text = message.text.strip()
    if not text.isdigit():
        await message.answer("❌ Telegram ID должен состоять только из цифр. Попробуйте еще раз:")
        return

    tgt_id = int(text)
    
    # Проверяем, существует ли уже
    chk = (await db_session.execute(select(Admin).where(Admin.telegram_id == tgt_id))).scalar_one_or_none()
    if chk:
        await message.answer("ℹ️ Этот пользователь уже есть в списке администраторов.")
        await state.clear()
        return

    new_admin = Admin(
        telegram_id=tgt_id,
        role=AdminRole.ADMIN,
        admin_status=AdminStatus.ACTIVE,
    )
    db_session.add(new_admin)
    await db_session.commit()

    await message.answer(f"✅ Администратор с Telegram ID `{tgt_id}` успешно добавлен!", parse_mode="Markdown")
    await state.clear()

@admin_router.callback_query(F.data.startswith("dev_del_admin_"))
async def dev_delete_admin_action(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    if not await _deny_unless_developer(callback, role):
        return
    await callback.answer()
    admin_db_id = int(callback.data.split("_")[3])

    await db_session.execute(delete(Admin).where(Admin.id == admin_db_id))
    await db_session.commit()
    await callback.message.answer("🗑 Администратор успешно удален из системы.")
    await dev_manage_admins_menu(callback, db_session, role)


# --- ОЧИСТКА АРХИВА РАЗРАБОТЧИКОМ ---
@admin_router.callback_query(F.data == "dev_clear_history")
async def dev_clear_history_action(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    if not await _deny_unless_developer(callback, role):
        return
    await callback.answer()
    try:
        # Чистим все завершенные турниры
        completed_tours = (await db_session.execute(
            select(Tournament.id).where(Tournament.status == TournamentStatus.COMPLETED)
        )).scalars().all()

        if not completed_tours:
            await callback.message.answer("ℹ️ Архив пуст. Нет завершенных турниров для удаления.")
            return

        for t_id in completed_tours:
            await delete_tournament_cascade(db_session, t_id)

        await db_session.commit()
        await callback.message.answer(f"🧹 История очищена! Удалено завершенных турниров: {len(completed_tours)}.")
    except Exception as e:
        await db_session.rollback()
        await callback.message.answer(f"❌ Ошибка очистки истории: {e}")


# --- ГЕНЕРАЦИЯ ТЕСТОВЫХ БОТОВ (25 ИГРОКОВ) ---
@admin_router.callback_query(F.data.startswith("test_fill_"))
async def test_fill_tournament_with_bots(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    if not await _deny_unless_developer(callback, role):
        return
    await callback.answer()
    tour_id = int(callback.data.split("_")[2])

    try:
        for i in range(1, 26):
            fake_tg_id = 1000000 + tour_id * 100 + i
            
            # Создаем или находим фейкового юзера
            chk_user = (await db_session.execute(select(User).where(User.telegram_id == fake_tg_id))).scalar_one_or_none()
            if not chk_user:
                chk_user = User(
                    telegram_id=fake_tg_id,
                    telegram_username=f"bot_player_{tour_id}_{i}",
                )
                db_session.add(chk_user)
                await db_session.flush()

            existing_reg = (await db_session.execute(
                select(Registration).where(
                    Registration.tournament_id == tour_id,
                    Registration.user_id == chk_user.id,
                )
            )).scalar_one_or_none()
            if existing_reg:
                continue

            reg = Registration(
                tournament_id=tour_id,
                user_id=chk_user.id,
                game_nick=f"Tester#{1000+i}",
                game_rank="Gold 2",
                contact_telegram=f"@bot_player_{tour_id}_{i}",
                status=RegistrationStatus.REGISTERED,
                subscription_status=SubscriptionStatus.SUBSCRIBED,
                rules_accepted=True,
            )
            db_session.add(reg)

        await db_session.commit()
        await callback.message.answer("🤖 Успешно сгенерировано 25 тестовых игроков!")
        await manage_single_tournament(callback, db_session, role)
    except Exception as e:
        await db_session.rollback()
        await callback.message.answer(f"❌ Ошибка генерации: {e}")


# --- УДАЛЕНИЕ ТЕСТОВЫХ БОТОВ ---
@admin_router.callback_query(F.data.startswith("test_clear_"))
async def test_clear_bots_action(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    if not await _deny_unless_developer(callback, role):
        return
    await callback.answer()
    tour_id = int(callback.data.split("_")[2])

    try:
        await db_session.execute(delete(Registration).where(Registration.tournament_id == tour_id))
        await db_session.commit()
        await callback.message.answer("🧹 Все заявки текущего турнира успешно очищены!")
        await manage_single_tournament(callback, db_session, role)
    except Exception as e:
        await db_session.rollback()
        await callback.message.answer(f"❌ Ошибка очистки: {e}")