from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from bot.services.scoring import (
    TIEBREAKER_LABELS,
    analyze_cutoff_tie,
    get_ranked_leaderboard,
    get_rating_tiebreakers,
)
from bot.utils.player_display import admin_player_label, public_player_label
from db.models import Finalist, FinalistSource, Registration, Tournament, TournamentStatus, User


def confirm_finalist_keyboard(finalist_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтверждаю участие в финале",
                    callback_data=f"confirm_finalist_{finalist_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отказаться от финала",
                    callback_data=f"decline_finalist_{finalist_id}",
                ),
            ]
        ]
    )


def decline_finalist_confirm_keyboard(finalist_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, отказаться",
                    callback_data=f"decline_finalist_confirm_{finalist_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"decline_finalist_cancel_{finalist_id}",
                ),
            ]
        ]
    )


async def get_finalists(db_session: AsyncSession, tour_id: int) -> list[tuple[Finalist, Registration]]:
    rows = (
        await db_session.execute(
            select(Finalist, Registration)
            .join(Registration, Registration.id == Finalist.registration_id)
            .where(Finalist.tournament_id == tour_id)
            .order_by(Finalist.id)
        )
    ).all()
    return list(rows)


async def format_finalists_text(
    db_session: AsyncSession,
    tour_id: int,
    *,
    admin_view: bool = False,
) -> str:
    finalists = await get_finalists(db_session, tour_id)
    if not finalists:
        return "🏅 Финалисты ещё не определены."

    lines = ["🏅 Список финалистов:\n"]
    for idx, (finalist, registration) in enumerate(finalists, start=1):
        label = admin_player_label(registration) if admin_view else public_player_label(registration)
        confirm = "✅" if finalist.participation_confirmed else "⏳"
        lines.append(f"{idx}. {label} {confirm}")
    return "\n".join(lines)


async def assign_finalists_from_rating(db_session: AsyncSession, tour_id: int) -> tuple[int, bool]:
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        raise ValueError("Турнир не найден.")

    existing = await db_session.scalar(
        select(Finalist.id).where(Finalist.tournament_id == tour_id).limit(1)
    )
    if existing:
        raise ValueError("Финалисты уже определены. Используйте замену для изменения состава.")

    ranked = await get_ranked_leaderboard(db_session, tour_id)
    if len(ranked) < tour.final_size:
        raise ValueError(
            f"Недостаточно игроков с баллами для финала: нужно {tour.final_size}, есть {len(ranked)}."
        )

    analysis = await analyze_cutoff_tie(db_session, tour_id, tour.final_size)
    if analysis["status"] == "need_tiebreaker":
        raise ValueError("tiebreaker_still_required")
    if analysis["status"] == "manual":
        raise ValueError("manual_selection_required")

    for _, registration, _ in ranked[: tour.final_size]:
        db_session.add(
            Finalist(
                tournament_id=tour_id,
                registration_id=registration.id,
                source=FinalistSource.RATING,
            )
        )

    tour.status = TournamentStatus.FINALISTS_SELECTED
    return tour.final_size, False


async def assign_manual_finalists(
    db_session: AsyncSession,
    tour_id: int,
    registration_ids: list[int],
) -> int:
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        raise ValueError("Турнир не найден.")

    existing = await db_session.scalar(
        select(Finalist.id).where(Finalist.tournament_id == tour_id).limit(1)
    )
    if existing:
        raise ValueError("Финалисты уже определены. Используйте замену для изменения состава.")

    if len(registration_ids) != tour.final_size:
        raise ValueError(
            f"Нужно выбрать ровно {tour.final_size} финалистов, выбрано {len(registration_ids)}."
        )

    from bot.services.scoring import get_manual_pick_context

    ctx = await get_manual_pick_context(db_session, tour_id, tour.final_size)
    auto_ids = {reg.id for reg in ctx["auto_finalists"]}
    tied_ids = {reg.id for reg, _ in ctx["tied"]}
    picked_from_tied = [reg_id for reg_id in registration_ids if reg_id in tied_ids]

    if len(picked_from_tied) != ctx["slots_to_pick"]:
        raise ValueError(
            f"Из группы с равным счётом нужно выбрать {ctx['slots_to_pick']} игрок(ов)."
        )
    if set(registration_ids) != auto_ids | set(picked_from_tied):
        raise ValueError("Неверный состав финалистов.")

    for registration_id in registration_ids:
        db_session.add(
            Finalist(
                tournament_id=tour_id,
                registration_id=registration_id,
                source=FinalistSource.RATING,
            )
        )

    tour.status = TournamentStatus.FINALISTS_SELECTED
    return len(registration_ids)


async def send_finalist_confirmations(
    db_session: AsyncSession,
    bot: Bot,
    tour_id: int,
) -> tuple[int, int]:
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        raise ValueError("Турнир не найден.")

    finalists = await get_finalists(db_session, tour_id)
    if not finalists:
        raise ValueError("Сначала определите финалистов.")

    sent = 0
    failed = 0
    for finalist, registration in finalists:
        user = (
            await db_session.execute(select(User).where(User.id == registration.user_id))
        ).scalar_one_or_none()
        if not user:
            failed += 1
            continue
        try:
            await bot.send_message(
                user.telegram_id,
                "🏅 Поздравляем! Вы вошли в топ финалистов турнира.\n"
                "Подтвердите участие в финале кнопкой ниже.",
                reply_markup=confirm_finalist_keyboard(finalist.id),
            )
            sent += 1
        except Exception:
            failed += 1
    return sent, failed


async def replace_finalist_with_next(
    db_session: AsyncSession,
    tour_id: int,
    finalist_id: int,
) -> Registration:
    finalist = (
        await db_session.execute(
            select(Finalist).where(
                Finalist.id == finalist_id,
                Finalist.tournament_id == tour_id,
            )
        )
    ).scalar_one_or_none()
    if not finalist:
        raise ValueError("Финалист не найден.")

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        raise ValueError("Турнир не найден.")

    current_finalist_ids = {
        item.registration_id
        for item in (
            await db_session.execute(select(Finalist).where(Finalist.tournament_id == tour_id))
        ).scalars().all()
    }
    ranked = await get_ranked_leaderboard(db_session, tour_id)
    replacement = next(
        (registration for _, registration, _ in ranked if registration.id not in current_finalist_ids),
        None,
    )
    if not replacement:
        raise ValueError("В рейтинге нет доступных игроков для замены.")

    finalist.registration_id = replacement.id
    finalist.participation_confirmed = False
    finalist.participation_confirmed_at = None
    return replacement


async def add_finalist_manual(
    db_session: AsyncSession,
    tour_id: int,
    registration_id: int,
) -> Finalist:
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        raise ValueError("Турнир не найден.")

    current_count = await db_session.scalar(
        select(func.count(Finalist.id)).where(Finalist.tournament_id == tour_id)
    ) or 0
    if current_count >= tour.final_size:
        raise ValueError(f"Список финалистов уже заполнен ({tour.final_size}).")

    reg = (
        await db_session.execute(
            select(Registration).where(
                Registration.id == registration_id,
                Registration.tournament_id == tour_id,
            )
        )
    ).scalar_one_or_none()
    if not reg:
        raise ValueError("Участник не найден в этом турнире.")

    existing = await db_session.scalar(
        select(Finalist.id).where(
            Finalist.tournament_id == tour_id,
            Finalist.registration_id == registration_id,
        ).limit(1)
    )
    if existing:
        raise ValueError("Участник уже в списке финалистов.")

    finalist = Finalist(
        tournament_id=tour_id,
        registration_id=registration_id,
        source=FinalistSource.MANUAL_ADMIN,
    )
    db_session.add(finalist)
    if tour.status == TournamentStatus.RATING_CALCULATED:
        tour.status = TournamentStatus.FINALISTS_SELECTED
    return finalist


async def remove_finalist_manual(
    db_session: AsyncSession,
    tour_id: int,
    finalist_id: int,
) -> None:
    finalist = (
        await db_session.execute(
            select(Finalist).where(
                Finalist.id == finalist_id,
                Finalist.tournament_id == tour_id,
            )
        )
    ).scalar_one_or_none()
    if not finalist:
        raise ValueError("Финалист не найден.")
    await db_session.delete(finalist)
