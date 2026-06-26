import secrets
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from bot.services.subscription import is_user_subscribed
from bot.services.tournament_meta import get_tournament_meta, save_tournament_meta
from bot.utils.player_display import admin_player_label
from bot.utils.test_bots import is_test_bot_user
from db.models import Registration, RegistrationStatus, SubscriptionStatus, Tournament, TournamentStatus, User


def _draft_key() -> str:
    return "selection_draft"


async def get_selection_draft(db_session: AsyncSession, tour_id: int) -> dict[str, Any] | None:
    meta = await get_tournament_meta(db_session, tour_id)
    return meta.get(_draft_key())


async def save_selection_draft(
    db_session: AsyncSession,
    tour_id: int,
    draft: dict[str, Any],
) -> None:
    meta = await get_tournament_meta(db_session, tour_id)
    meta[_draft_key()] = draft
    await save_tournament_meta(db_session, tour_id, meta)


async def clear_selection_draft(db_session: AsyncSession, tour_id: int) -> None:
    meta = await get_tournament_meta(db_session, tour_id)
    meta.pop(_draft_key(), None)
    await save_tournament_meta(db_session, tour_id, meta)


async def get_selection_candidates(
    db_session: AsyncSession,
    tour_id: int,
) -> list[Registration]:
    return list(
        (
            await db_session.execute(
                select(Registration).where(
                    Registration.tournament_id == tour_id,
                    Registration.status == RegistrationStatus.REGISTERED,
                    Registration.subscription_status == SubscriptionStatus.SUBSCRIBED,
                    Registration.rules_accepted.is_(True),
                ).order_by(Registration.id)
            )
        ).scalars().all()
    )


async def build_random_draft(
    db_session: AsyncSession,
    bot: Bot,
    tour: Tournament,
    main_count: int,
    reserve_count: int,
) -> dict[str, Any]:
    candidates = await get_selection_candidates(db_session, tour.id)
    user_ids = [reg.user_id for reg in candidates]
    users_by_id = {}
    if user_ids:
        users = (
            await db_session.execute(select(User).where(User.id.in_(user_ids)))
        ).scalars().all()
        users_by_id = {user.id: user for user in users}

    subscribed: list[Registration] = []
    for reg in candidates:
        user = users_by_id.get(reg.user_id)
        if not user:
            continue
        if is_test_bot_user(
            telegram_id=user.telegram_id,
            username=user.telegram_username,
        ):
            reg.subscription_status = SubscriptionStatus.SUBSCRIBED
            subscribed.append(reg)
            continue
        if await is_user_subscribed(
            bot,
            tour.channel_id,
            user.telegram_id,
            channel_username=tour.channel_username,
        ):
            reg.subscription_status = SubscriptionStatus.SUBSCRIBED
            subscribed.append(reg)
        else:
            reg.subscription_status = SubscriptionStatus.UNSUBSCRIBED

    if len(subscribed) < main_count + reserve_count:
        raise ValueError(
            f"Недостаточно подписанных участников: нужно {main_count + reserve_count}, "
            f"доступно {len(subscribed)}."
        )

    rng = secrets.SystemRandom()
    shuffled = list(subscribed)
    rng.shuffle(shuffled)
    main_ids = [r.id for r in shuffled[:main_count]]
    reserve_ids = [r.id for r in shuffled[main_count:main_count + reserve_count]]
    not_selected_ids = [r.id for r in shuffled[main_count + reserve_count:]]
    return {
        "main_count": main_count,
        "reserve_count": reserve_count,
        "main_ids": main_ids,
        "reserve_ids": reserve_ids,
        "not_selected_ids": not_selected_ids,
    }


async def format_draft_text(db_session: AsyncSession, draft: dict[str, Any]) -> str:
    async def _labels(ids: list[int]) -> list[str]:
        if not ids:
            return ["—"]
        rows = (
            await db_session.execute(select(Registration).where(Registration.id.in_(ids)))
        ).scalars().all()
        by_id = {row.id: row for row in rows}
        return [admin_player_label(by_id[i]) for i in ids if i in by_id]

    main_lines = await _labels(draft.get("main_ids", []))
    reserve_lines = await _labels(draft.get("reserve_ids", []))
    rejected_lines = await _labels(draft.get("not_selected_ids", []))
    return (
        f"🎲 Черновик отбора\n\n"
        f"🟢 Основной состав ({len(draft.get('main_ids', []))}):\n"
        + "\n".join(f"• {line}" for line in main_lines)
        + f"\n\n🔵 Резерв ({len(draft.get('reserve_ids', []))}):\n"
        + "\n".join(f"• {line}" for line in reserve_lines)
        + f"\n\n⚪ Не прошли отбор ({len(draft.get('not_selected_ids', []))}):\n"
        + "\n".join(f"• {line}" for line in rejected_lines)
    )


def draft_preview_keyboard(tour_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Переотобрать", callback_data=f"sel_reroll_{tour_id}")],
            [
                InlineKeyboardButton(text="✏️ Основной", callback_data=f"sel_edit_main_{tour_id}"),
                InlineKeyboardButton(text="✏️ Резерв", callback_data=f"sel_edit_reserve_{tour_id}"),
            ],
            [InlineKeyboardButton(text="✅ Подтвердить отбор", callback_data=f"sel_confirm_{tour_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"manage_tour_{tour_id}")],
        ]
    )


async def apply_selection_draft(
    db_session: AsyncSession,
    bot: Bot,
    tour_id: int,
    draft: dict[str, Any],
) -> None:
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        raise ValueError("Турнир не найден.")

    all_regs = (
        await db_session.execute(select(Registration).where(Registration.tournament_id == tour_id))
    ).scalars().all()
    main_ids = set(draft.get("main_ids", []))
    reserve_ids = set(draft.get("reserve_ids", []))
    not_selected_ids = set(draft.get("not_selected_ids", []))

    for reg in all_regs:
        if reg.id in main_ids:
            reg.status = RegistrationStatus.SELECTED_MAIN
            reg.participation_confirmed = False
            reg.participation_confirmed_at = None
        elif reg.id in reserve_ids:
            reg.status = RegistrationStatus.SELECTED_RESERVE
            reg.participation_confirmed = False
            reg.participation_confirmed_at = None
        elif reg.id in not_selected_ids:
            reg.status = RegistrationStatus.NOT_SELECTED
        elif reg.status == RegistrationStatus.REGISTERED:
            reg.status = RegistrationStatus.NOT_SELECTED

    tour.main_slots = draft.get("main_count", tour.main_slots)
    tour.reserve_slots = draft.get("reserve_count", tour.reserve_slots)
    tour.status = TournamentStatus.SELECTION_DONE
    await clear_selection_draft(db_session, tour_id)
    await notify_selection_results(db_session, bot, tour)


async def notify_selection_results(
    db_session: AsyncSession,
    bot: Bot,
    tour: Tournament,
) -> None:
    registrations = (
        await db_session.execute(select(Registration).where(Registration.tournament_id == tour.id))
    ).scalars().all()
    for reg in registrations:
        user_tg = (
            await db_session.execute(select(User.telegram_id).where(User.id == reg.user_id))
        ).scalar_one_or_none()
        if not user_tg:
            continue
        if reg.status == RegistrationStatus.SELECTED_MAIN:
            text = (
                f"🎉 Поздравляем! Вы в основном составе турнира «{tour.title}».\n"
                "Ожидайте запроса на подтверждение участия от администратора."
            )
        elif reg.status == RegistrationStatus.SELECTED_RESERVE:
            text = (
                f"🔵 Вы в резерве турнира «{tour.title}».\n"
                "При освобождении места администратор может включить вас в основной состав."
            )
        elif reg.status == RegistrationStatus.NOT_SELECTED:
            text = f"К сожалению, вы не прошли отбор на турнир «{tour.title}»."
        else:
            continue
        try:
            await bot.send_message(user_tg, text)
        except Exception:
            pass


async def swap_registration_in_draft(
    db_session: AsyncSession,
    tour_id: int,
    reg_id: int,
    target_list: str,
) -> dict[str, Any]:
    draft = await get_selection_draft(db_session, tour_id)
    if not draft:
        raise ValueError("Черновик отбора не найден. Запустите отбор заново.")

    lists = {
        "main": set(draft.get("main_ids", [])),
        "reserve": set(draft.get("reserve_ids", [])),
        "not_selected": set(draft.get("not_selected_ids", [])),
    }
    current = next((name for name, ids in lists.items() if reg_id in ids), None)
    if not current:
        raise ValueError("Участник не найден в черновике.")

    if target_list not in lists or target_list == current:
        raise ValueError("Некорректный список.")

    if target_list == "main" and len(lists["main"]) >= draft["main_count"]:
        raise ValueError("Основной состав уже заполнен.")
    if target_list == "reserve" and len(lists["reserve"]) >= draft["reserve_count"]:
        raise ValueError("Резерв уже заполнен.")

    lists[current].remove(reg_id)
    lists[target_list].add(reg_id)
    draft["main_ids"] = list(lists["main"])
    draft["reserve_ids"] = list(lists["reserve"])
    draft["not_selected_ids"] = list(lists["not_selected"])
    await save_selection_draft(db_session, tour_id, draft)
    return draft
