"""Личная история участника: баллы, место в рейтинге и роль в финале."""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.final_stage import get_tournament_winner
from bot.services.scoring import get_ranked_leaderboard
from bot.utils.player_display import public_player_label
from bot.utils.timezone import format_moscow_date
from db.models import (
    Finalist,
    MvpAward,
    Registration,
    Stage,
    StageResult,
    StageStatus,
    TeamLabel,
    Tournament,
    TournamentStatus,
    User,
)


def _tournament_date(tour: Tournament) -> str:
    """Дата турнира для отображения: завершение или создание."""
    if tour.completed_at:
        return format_moscow_date(tour.completed_at)
    if tour.created_at:
        return format_moscow_date(tour.created_at)
    return "—"


async def _final_role_lines(
    db_session: AsyncSession,
    tour_id: int,
    registration_id: int,
) -> list[str]:
    """Роли участника в финале: команда, MVP, победа/поражение."""
    final_stage = (
        await db_session.execute(
            select(Stage).where(
                Stage.tournament_id == tour_id,
                Stage.is_final.is_(True),
                Stage.status == StageStatus.COMPLETED,
            )
        )
    ).scalar_one_or_none()
    if not final_stage:
        return []

    final_result = (
        await db_session.execute(
            select(StageResult).where(
                StageResult.stage_id == final_stage.id,
                StageResult.registration_id == registration_id,
            )
        )
    ).scalar_one_or_none()

    mvp_awards = (
        await db_session.execute(
            select(MvpAward).where(MvpAward.tournament_id == tour_id)
        )
    ).scalars().all()
    mvp_by_team = {award.team_label: award for award in mvp_awards}

    lines: list[str] = []
    if final_result:
        if final_result.placement == 1:
            lines.append("🏆 Участник победившей команды финала")
        elif final_result.placement == 2:
            lines.append("Участник проигравшей команды финала")
        else:
            lines.append(f"Финал · команда {final_result.team_label.value}")

    for label in (TeamLabel.A, TeamLabel.B):
        award = mvp_by_team.get(label)
        if not award or award.registration_id != registration_id:
            continue
        side = "победившей" if award.is_tournament_winner else "проигравшей"
        lines.append(f"⭐ MVP {side} команды финала")

    finalist = (
        await db_session.execute(
            select(Finalist).where(
                Finalist.tournament_id == tour_id,
                Finalist.registration_id == registration_id,
            )
        )
    ).scalar_one_or_none()
    if finalist and not final_result:
        lines.append("🎯 Приглашён в финал")

    return lines


async def format_user_personal_history(
    db_session: AsyncSession,
    telegram_id: int,
) -> str:
    """
    Список турниров пользователя с групповыми баллами, местом до финала
    и итогами финала (команда, MVP).
    """
    registrations = (
        await db_session.execute(
            select(Registration, Tournament)
            .join(Tournament, Tournament.id == Registration.tournament_id)
            .join(User, User.id == Registration.user_id)
            .where(User.telegram_id == telegram_id)
            .order_by(Tournament.completed_at.desc(), Tournament.id.desc())
        )
    ).all()

    if not registrations:
        return "🏅 Личная история пуста — вы ещё не участвовали в турнирах."

    lines = ["🏅 Ваша личная история турниров:\n"]
    seen_tours: set[int] = set()

    for registration, tour in registrations:
        if tour.id in seen_tours:
            continue
        seen_tours.add(tour.id)

        group_points = await db_session.scalar(
            select(func.coalesce(func.sum(StageResult.points), 0))
            .join(Stage, Stage.id == StageResult.stage_id)
            .where(
                StageResult.registration_id == registration.id,
                Stage.tournament_id == tour.id,
                Stage.is_final.is_(False),
            )
        ) or Decimal("0")

        ranked = await get_ranked_leaderboard(
            db_session, tour.id, group_stage_only=True
        )
        place = next(
            (item[0] for item in ranked if item[1].id == registration.id),
            None,
        )

        status_note = ""
        if tour.status == TournamentStatus.COMPLETED:
            status_note = " · завершён"
        elif tour.status not in (
            TournamentStatus.CANCELLED,
            TournamentStatus.DRAFT,
        ):
            status_note = " · идёт"

        lines.append(f"\n🏆 {tour.title}{status_note}")
        lines.append(f"   📅 {_tournament_date(tour)}")
        lines.append(f"   📊 Баллы группового этапа: {group_points}")
        if place:
            lines.append(f"   📈 Место в рейтинге до финала: {place}")
        else:
            lines.append("   📈 Место в рейтинге до финала: —")

        final_lines = await _final_role_lines(db_session, tour.id, registration.id)
        for note in final_lines:
            lines.append(f"   {note}")

        if tour.status == TournamentStatus.COMPLETED:
            winner = await get_tournament_winner(db_session, tour.id)
            if winner:
                lines.append(
                    f"   🥇 Победитель турнира: {public_player_label(winner)}"
                )

    lines.append(
        f"\n🕐 Время указано по Москве (MSK)."
    )
    return "\n".join(lines)
