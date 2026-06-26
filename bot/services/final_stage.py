import secrets

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.scoring import PlayerMatchStats, save_stage_match_results
from bot.services.stages import get_stage_teams_with_members
from bot.services.tournament_meta import get_tournament_meta, save_tournament_meta
from bot.utils.player_display import admin_player_label, public_player_label
from bot.utils.timezone import now_moscow
from db.models import (
    Finalist,
    MvpAward,
    Registration,
    Stage,
    StageResult,
    StageStatus,
    StageTeam,
    StageTeamMember,
    TeamLabel,
    Tournament,
    TournamentStatus,
    User,
)


async def get_finalists_confirmation_stats(
    db_session: AsyncSession, tour_id: int
) -> tuple[int, int]:
    rows = await db_session.execute(
        select(Finalist).where(Finalist.tournament_id == tour_id)
    )
    finalists = list(rows.scalars().all())
    if not finalists:
        return 0, 0
    confirmed = sum(1 for item in finalists if item.participation_confirmed)
    return confirmed, len(finalists)


async def all_finalists_confirmed(db_session: AsyncSession, tour_id: int) -> bool:
    confirmed, total = await get_finalists_confirmation_stats(db_session, tour_id)
    return total > 0 and confirmed == total


async def confirm_all_finalists_dev(db_session: AsyncSession, tour_id: int) -> int:
    finalists = (
        await db_session.execute(select(Finalist).where(Finalist.tournament_id == tour_id))
    ).scalars().all()
    counter = 0
    for finalist in finalists:
        if not finalist.participation_confirmed:
            finalist.participation_confirmed = True
            finalist.participation_confirmed_at = now_moscow()
            counter += 1
    return counter


async def get_final_stage(db_session: AsyncSession, tour_id: int) -> Stage | None:
    return (
        await db_session.execute(
            select(Stage)
            .where(Stage.tournament_id == tour_id, Stage.is_final.is_(True))
            .order_by(Stage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_final_stage_teams(db_session: AsyncSession, tour: Tournament) -> Stage:
    if not await all_finalists_confirmed(db_session, tour.id):
        raise ValueError("Не все финалисты подтвердили участие.")

    existing = await get_final_stage(db_session, tour.id)
    if existing and existing.status != StageStatus.COMPLETED:
        raise ValueError("Команды финала уже сформированы.")

    rows = (
        await db_session.execute(
            select(Finalist, Registration)
            .join(Registration, Registration.id == Finalist.registration_id)
            .where(
                Finalist.tournament_id == tour.id,
                Finalist.participation_confirmed.is_(True),
            )
            .order_by(Finalist.id)
        )
    ).all()
    members = [registration for _, registration in rows]
    expected = tour.final_size or 10
    if len(members) != expected:
        raise ValueError(f"Для финала нужно {expected} подтвердивших финалистов, сейчас {len(members)}.")

    subgroup_size = tour.subgroup_size or 5
    stage_number = (
        await db_session.scalar(
            select(func.count(Stage.id)).where(Stage.tournament_id == tour.id)
        )
        or 0
    ) + 1

    stage = Stage(
        tournament_id=tour.id,
        group_id=None,
        stage_number=stage_number,
        is_final=True,
        status=StageStatus.PENDING,
    )
    db_session.add(stage)
    await db_session.flush()

    shuffled = list(members)
    secrets.SystemRandom().shuffle(shuffled)
    team_a = shuffled[:subgroup_size]
    team_b = shuffled[subgroup_size : subgroup_size * 2]

    for label, team_members in ((TeamLabel.A, team_a), (TeamLabel.B, team_b)):
        stage_team = StageTeam(stage_id=stage.id, team_label=label)
        db_session.add(stage_team)
        await db_session.flush()
        for member in team_members:
            db_session.add(
                StageTeamMember(stage_team_id=stage_team.id, registration_id=member.id)
            )

    stage.status = StageStatus.TEAMS_FORMED
    tour.status = TournamentStatus.FINAL_IN_PROGRESS
    return stage


async def send_final_code(
    db_session: AsyncSession,
    bot: Bot,
    stage_id: int,
    code: str,
) -> tuple[int, int]:
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage or not stage.is_final:
        raise ValueError("Финальный этап не найден.")
    if stage.status not in (StageStatus.TEAMS_FORMED, StageStatus.CODE_SENT):
        raise ValueError("Сначала сформируйте команды финала.")

    stage.match_code = code.strip()
    stage.code_sent_at = now_moscow()
    stage.status = StageStatus.CODE_SENT

    teams = await get_stage_teams_with_members(db_session, stage_id)
    sent = 0
    failed = 0
    for members in teams.values():
        for member in members:
            user = (
                await db_session.execute(select(User).where(User.id == member.user_id))
            ).scalar_one_or_none()
            if not user:
                failed += 1
                continue
            try:
                await bot.send_message(
                    user.telegram_id,
                    f"🏆 Код лобби финала:\n\n"
                    f"<code>{stage.match_code}</code> (код копируется при нажатии)",
                    parse_mode="HTML",
                )
                sent += 1
            except Exception:
                failed += 1
    return sent, failed


async def save_final_match_results(
    db_session: AsyncSession,
    stage_id: int,
    winning_team: TeamLabel,
    mvp_team_a_id: int,
    mvp_team_b_id: int,
    player_stats: dict[int, PlayerMatchStats],
    admin_id: int | None = None,
) -> Registration:
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage or not stage.is_final:
        raise ValueError("Финальный этап не найден.")

    await save_stage_match_results(
        db_session,
        stage_id,
        winning_team,
        mvp_team_a_id,
        mvp_team_b_id,
        player_stats,
        admin_id=admin_id,
    )

    winner_mvp_id = mvp_team_a_id if winning_team == TeamLabel.A else mvp_team_b_id
    for team_label, mvp_id in ((TeamLabel.A, mvp_team_a_id), (TeamLabel.B, mvp_team_b_id)):
        existing = (
            await db_session.execute(
                select(MvpAward).where(
                    MvpAward.tournament_id == stage.tournament_id,
                    MvpAward.team_label == team_label,
                )
            )
        ).scalar_one_or_none()
        if existing:
            await db_session.delete(existing)
        db_session.add(
            MvpAward(
                tournament_id=stage.tournament_id,
                team_label=team_label,
                registration_id=mvp_id,
                is_tournament_winner=mvp_id == winner_mvp_id,
            )
        )

    meta = await get_tournament_meta(db_session, stage.tournament_id)
    meta["final_completed"] = True
    await save_tournament_meta(db_session, stage.tournament_id, meta)

    return (
        await db_session.execute(
            select(Registration).where(Registration.id == winner_mvp_id)
        )
    ).scalar_one()


async def sync_final_awards_from_stage(db_session: AsyncSession, stage_id: int) -> None:
    from bot.services.scoring import get_stage_result_context

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage or not stage.is_final:
        return

    ctx = await get_stage_result_context(db_session, stage_id)
    winning_team = ctx["winning_team"]
    mvp_a_id = ctx["mvp_a_id"]
    mvp_b_id = ctx["mvp_b_id"]
    if not mvp_a_id or not mvp_b_id:
        raise ValueError("Для финала должны быть назначены MVP обеих команд.")

    winner_mvp_id = mvp_a_id if winning_team == TeamLabel.A else mvp_b_id
    for team_label, mvp_id in ((TeamLabel.A, mvp_a_id), (TeamLabel.B, mvp_b_id)):
        existing = (
            await db_session.execute(
                select(MvpAward).where(
                    MvpAward.tournament_id == stage.tournament_id,
                    MvpAward.team_label == team_label,
                )
            )
        ).scalar_one_or_none()
        if existing:
            await db_session.delete(existing)
        db_session.add(
            MvpAward(
                tournament_id=stage.tournament_id,
                team_label=team_label,
                registration_id=mvp_id,
                is_tournament_winner=mvp_id == winner_mvp_id,
            )
        )

    meta = await get_tournament_meta(db_session, stage.tournament_id)
    meta["final_completed"] = True
    await save_tournament_meta(db_session, stage.tournament_id, meta)


async def get_tournament_winner(db_session: AsyncSession, tour_id: int) -> Registration | None:
    award = (
        await db_session.execute(
            select(MvpAward).where(
                MvpAward.tournament_id == tour_id,
                MvpAward.is_tournament_winner.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not award:
        return None
    return (
        await db_session.execute(
            select(Registration).where(Registration.id == award.registration_id)
        )
    ).scalar_one_or_none()


async def format_final_summary_text(
    db_session: AsyncSession,
    tour_id: int,
    *,
    admin_view: bool = False,
) -> str:
    stage = await get_final_stage(db_session, tour_id)
    if not stage:
        return ""

    meta = await get_tournament_meta(db_session, tour_id)
    if not meta.get("final_completed"):
        return ""

    teams = await get_stage_teams_with_members(db_session, stage.id)
    winner_label = (
        await db_session.execute(
            select(StageResult.team_label)
            .where(
                StageResult.stage_id == stage.id,
                StageResult.placement == 1,
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    label_fn = admin_player_label if admin_view else public_player_label
    lines = ["\n🏆 Итоги финала:\n"]

    if not winner_label:
        lines.append("Результат финала не найден.")

    for label in (TeamLabel.A, TeamLabel.B):
        suffix = " 🏆" if winner_label == label else ""
        lines.append(f"\nКоманда {label.value}{suffix}:")
        for member in teams[label]:
            lines.append(f"  • {label_fn(member)}")

    awards = (
        await db_session.execute(
            select(MvpAward, Registration)
            .join(Registration, Registration.id == MvpAward.registration_id)
            .where(MvpAward.tournament_id == tour_id)
            .order_by(MvpAward.team_label)
        )
    ).all()
    if awards:
        lines.append("")
        for award, registration in awards:
            mvp_label = label_fn(registration)
            winner_suffix = " — победитель турнира" if award.is_tournament_winner else ""
            lines.append(f"MVP команды {award.team_label.value}: {mvp_label}{winner_suffix}")

    return "\n".join(lines)


async def format_winner_text(
    db_session: AsyncSession,
    tour_id: int,
    *,
    admin_view: bool = False,
) -> str:
    summary = await format_final_summary_text(db_session, tour_id, admin_view=admin_view)
    if summary:
        return summary.strip()

    winner = await get_tournament_winner(db_session, tour_id)
    if not winner:
        return "🏆 Победитель финала ещё не определён."

    label = admin_player_label(winner) if admin_view else public_player_label(winner)
    return f"🏆 Победитель турнира: {label}"


async def notify_admins_finalist_declined(
    bot: Bot,
    db_session: AsyncSession,
    tour_id: int,
    registration: Registration,
) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from config import settings
    from db.models import Admin, AdminStatus, Tournament

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        return

    admin_ids = set(
        (
            await db_session.execute(
                select(Admin.telegram_id).where(Admin.admin_status == AdminStatus.ACTIVE)
            )
        ).scalars().all()
    )
    admin_ids.add(settings.DEVELOPER_TG_ID)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="📋 Финалисты и рейтинг",
                callback_data=f"finalists_view_{tour_id}",
            )
        ]]
    )
    text = (
        f"❌ Финалист {registration.contact_telegram} ({registration.game_nick}) "
        f"отказался от участия в финале турнира «{tour.title}».\n"
        "Замену нужно выполнить вручную."
    )
    for telegram_id in admin_ids:
        try:
            await bot.send_message(telegram_id, text, reply_markup=keyboard)
        except Exception:
            pass


async def notify_admins_all_finalists_confirmed(
    bot,
    db_session: AsyncSession,
    tour_id: int,
) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from config import settings
    from db.models import Admin, AdminStatus, Tournament, TournamentStatus

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour or tour.status != TournamentStatus.FINALISTS_SELECTED:
        return
    if not await all_finalists_confirmed(db_session, tour_id):
        return

    admin_ids = set(
        (
            await db_session.execute(
                select(Admin.telegram_id).where(Admin.admin_status == AdminStatus.ACTIVE)
            )
        ).scalars().all()
    )
    admin_ids.add(settings.DEVELOPER_TG_ID)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🏆 Перейти к управлению финалом",
                callback_data=f"final_dash_{tour_id}",
            )
        ]]
    )
    text = (
        f"✅ Все финалисты турнира «{tour.title}» подтвердили участие в финале.\n"
        "Можно переходить к формированию команд финала."
    )
    for telegram_id in admin_ids:
        try:
            await bot.send_message(telegram_id, text, reply_markup=keyboard)
        except Exception:
            pass


async def complete_tournament(db_session: AsyncSession, tour_id: int) -> None:
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        raise ValueError("Турнир не найден.")
    meta = await get_tournament_meta(db_session, tour_id)
    if not meta.get("final_completed"):
        raise ValueError("Сначала внесите результаты финала.")
    tour.status = TournamentStatus.COMPLETED
    tour.completed_at = now_moscow()
    if not meta.get("group_stage_finished"):
        meta["group_stage_finished"] = True
        await save_tournament_meta(db_session, tour_id, meta)
