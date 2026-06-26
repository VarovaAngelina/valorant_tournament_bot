"""Архив завершённых турниров и детальная история."""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from bot.services.final_stage import format_final_summary_text, get_tournament_winner
from bot.services.scoring import format_leaderboard_text
from bot.utils.player_display import admin_player_label, public_player_label
from bot.utils.timezone import format_moscow, format_moscow_date
from db.models import Admin, Registration, ReplacementLog, Stage, Tournament, TournamentStatus


async def get_completed_tournaments(db_session: AsyncSession) -> list[Tournament]:
    result = await db_session.execute(
        select(Tournament)
        .where(Tournament.status == TournamentStatus.COMPLETED)
        .order_by(Tournament.completed_at.desc(), Tournament.id.desc())
    )
    return list(result.scalars().all())


async def format_archive_list_text(
    db_session: AsyncSession,
    *,
    admin_view: bool = False,
) -> str:
    tournaments = await get_completed_tournaments(db_session)
    if not tournaments:
        return "📜 Архив турниров пуст. Завершённых турниров пока нет."

    label_fn = admin_player_label if admin_view else public_player_label
    lines = ["📜 История завершённых турниров:\n"]
    for idx, tour in enumerate(tournaments, start=1):
        winner = await get_tournament_winner(db_session, tour.id)
        winner_line = f" — 🥇 {label_fn(winner)}" if winner else ""
        date_line = format_moscow_date(tour.completed_at) if tour.completed_at else "—"
        lines.append(f"{idx}. 🏆 {tour.title}{winner_line}\n   Завершён: {date_line}")
    return "\n".join(lines)


async def format_replacement_history_text(
    db_session: AsyncSession,
    tour_id: int,
    *,
    admin_view: bool = False,
) -> str:
    logs = (
        await db_session.execute(
            select(ReplacementLog)
            .where(ReplacementLog.tournament_id == tour_id)
            .order_by(ReplacementLog.replaced_at.asc(), ReplacementLog.id.asc())
        )
    ).scalars().all()
    if not logs:
        return "♻️ Замены: не было."

    label_fn = admin_player_label if admin_view else public_player_label
    lines = ["♻️ История замен:\n"]
    for log in logs:
        old_reg = (
            await db_session.execute(
                select(Registration).where(Registration.id == log.old_registration_id)
            )
        ).scalar_one_or_none()
        new_reg = (
            await db_session.execute(
                select(Registration).where(Registration.id == log.new_registration_id)
            )
        ).scalar_one_or_none()
        if not old_reg or not new_reg:
            continue

        stage_hint = ""
        if log.from_stage_id:
            stage = (
                await db_session.execute(
                    select(Stage).where(Stage.id == log.from_stage_id)
                )
            ).scalar_one_or_none()
            if stage:
                stage_hint = f", с этапа #{stage.stage_number}"

        admin_hint = ""
        if log.replaced_by_admin_id:
            admin = (
                await db_session.execute(
                    select(Admin).where(Admin.id == log.replaced_by_admin_id)
                )
            ).scalar_one_or_none()
            if admin:
                admin_hint = f" · админ: {admin.display_name or admin.telegram_id}"

        date_line = format_moscow(log.replaced_at)
        lines.append(
            f"• {date_line}: {label_fn(old_reg)} → {label_fn(new_reg)}{stage_hint}{admin_hint}"
        )
    return "\n".join(lines)


async def format_tournament_history_detail(
    db_session: AsyncSession,
    tour_id: int,
    *,
    admin_view: bool = True,
) -> str:
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        return "❌ Турнир не найден."
    if tour.status != TournamentStatus.COMPLETED:
        return "ℹ️ Подробная история доступна только для завершённых турниров."

    label_fn = admin_player_label if admin_view else public_player_label
    lines = [f"📜 {tour.title}\n"]
    if tour.completed_at:
        lines.append(f"Завершён: {format_moscow(tour.completed_at)}\n")

    winner = await get_tournament_winner(db_session, tour_id)
    if winner:
        lines.append(f"🥇 Победитель турнира: {label_fn(winner)}\n")

    final_summary = await format_final_summary_text(
        db_session, tour_id, admin_view=admin_view
    )
    if final_summary:
        lines.append(final_summary)

    lines.append(await format_replacement_history_text(db_session, tour_id, admin_view=admin_view))
    lines.append("")
    lines.append(
        await format_leaderboard_text(
            db_session,
            tour_id,
            limit=10,
            admin_view=admin_view,
            group_stage_only=True,
        )
    )
    return "\n".join(lines).strip()
