from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from bot.services.final_stage import format_final_summary_text, get_tournament_winner
from bot.services.scoring import format_leaderboard_text, format_personal_stats_text
from bot.utils.player_display import public_player_label
from db.models import Registration, RegistrationStatus, User


async def _registration_telegram_id(db_session: AsyncSession, registration: Registration) -> int | None:
    return (
        await db_session.execute(select(User.telegram_id).where(User.id == registration.user_id))
    ).scalar_one_or_none()


async def notify_group_stage_rating_to_participants(
    bot: Bot,
    db_session: AsyncSession,
    tour_id: int,
) -> tuple[int, int]:
    registrations = (
        await db_session.execute(
            select(Registration).where(
                Registration.tournament_id == tour_id,
                Registration.status.in_(
                    (
                        RegistrationStatus.SELECTED_MAIN,
                        RegistrationStatus.SELECTED_RESERVE,
                    )
                ),
            )
        )
    ).scalars().all()

    sent = 0
    failed = 0
    for registration in registrations:
        user_tg = await _registration_telegram_id(db_session, registration)
        if not user_tg:
            failed += 1
            continue
        personal = await format_personal_stats_text(db_session, tour_id, user_tg)
        text = (
            "🏁 Групповой этап завершён.\n\n"
            f"{personal}"
        )
        try:
            await bot.send_message(user_tg, text)
            sent += 1
        except Exception:
            failed += 1
    return sent, failed


async def notify_final_results_to_participants(
    bot: Bot,
    db_session: AsyncSession,
    tour_id: int,
) -> tuple[int, int]:
    winner = await get_tournament_winner(db_session, tour_id)
    summary = await format_final_summary_text(db_session, tour_id, admin_view=False)
    leaderboard = await format_leaderboard_text(
        db_session,
        tour_id,
        limit=10,
        admin_view=False,
        group_stage_only=True,
    )

    registrations = (
        await db_session.execute(
            select(Registration).where(
                Registration.tournament_id == tour_id,
                Registration.status.in_(
                    (
                        RegistrationStatus.SELECTED_MAIN,
                        RegistrationStatus.SELECTED_RESERVE,
                        RegistrationStatus.NOT_SELECTED,
                    )
                ),
            )
        )
    ).scalars().all()

    sent = 0
    failed = 0
    for registration in registrations:
        user_tg = await _registration_telegram_id(db_session, registration)
        if not user_tg:
            failed += 1
            continue

        if winner and registration.id == winner.id:
            text = (
                "🎉 Поздравляем! Вы победили в турнире!\n\n"
                f"{summary}\n\n"
                "Мы свяжемся с вами для обсуждения приза и дальнейших шагов."
            )
        else:
            text = (
                "🏆 Опубликованы итоги финала турнира.\n\n"
                f"{summary}\n\n"
                f"{leaderboard}"
            )
        try:
            await bot.send_message(user_tg, text)
            sent += 1
        except Exception:
            failed += 1
    return sent, failed
