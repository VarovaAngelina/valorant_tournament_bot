"""Групповой этап: команды, коды лобби, прогресс раундов."""

import secrets
from datetime import datetime
from typing import Any

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.grouping import get_group_members, get_tournament_groups
from bot.utils.player_display import admin_player_label, public_player_label
from bot.utils.timezone import now_moscow
from bot.services.tournament_meta import get_tournament_meta, save_tournament_meta
from db.models import (
    Registration,
    Stage,
    StageStatus,
    StageTeam,
    StageTeamMember,
    TeamLabel,
    Tournament,
    TournamentStatus,
    User,
)

MAX_GROUP_CYCLES = 2  # исторический дефолт; фактически кругов может быть больше по решению админа


def get_approved_cycles(meta: dict) -> int:
    if "approved_cycles" in meta:
        return max(1, int(meta["approved_cycles"]))
    if meta.get("second_cycle_approved"):
        return 2
    return 1


def completed_cycles_count(completed_stages: list[Stage], num_groups: int) -> int:
    if num_groups <= 0:
        return 0
    return len(completed_stages) // num_groups


def cycle_for_stage_number(stage_number: int, num_groups: int) -> int:
    if num_groups <= 0:
        return 1
    return (stage_number - 1) // num_groups + 1


def group_index_for_stage_number(stage_number: int, num_groups: int) -> int:
    return (stage_number - 1) % num_groups


async def get_tournament_stages(db_session: AsyncSession, tour_id: int) -> list[Stage]:
    result = await db_session.execute(
        select(Stage).where(Stage.tournament_id == tour_id).order_by(Stage.stage_number)
    )
    return list(result.scalars().all())


def resolve_subgroup_size(tour: Tournament | None) -> int:
    if not tour or not tour.subgroup_size:
        return 5
    return tour.subgroup_size


async def get_stage_team_members(
    db_session: AsyncSession,
    stage_team_id: int,
) -> list[Registration]:
    result = await db_session.execute(
        select(Registration)
        .join(StageTeamMember, StageTeamMember.registration_id == Registration.id)
        .where(StageTeamMember.stage_team_id == stage_team_id)
        .order_by(Registration.id)
    )
    return list(result.scalars().all())


async def prune_stage_team_roster(db_session: AsyncSession, stage_id: int) -> int:
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage or not stage.group_id:
        return 0

    valid_reg_ids = {
        member.id for member in await get_group_members(db_session, stage.group_id)
    }
    stage_team_ids = (
        await db_session.execute(
            select(StageTeam.id).where(StageTeam.stage_id == stage_id)
        )
    ).scalars().all()
    if not stage_team_ids:
        return 0

    team_members = (
        await db_session.execute(
            select(StageTeamMember).where(
                StageTeamMember.stage_team_id.in_(stage_team_ids)
            )
        )
    ).scalars().all()

    seen_registration_ids: set[int] = set()
    removed = 0
    for team_member in team_members:
        should_remove = team_member.registration_id not in valid_reg_ids
        if not should_remove:
            if team_member.registration_id in seen_registration_ids:
                should_remove = True
            else:
                seen_registration_ids.add(team_member.registration_id)
        if should_remove:
            await db_session.delete(team_member)
            removed += 1
    return removed


async def get_stage_teams_with_members(
    db_session: AsyncSession, stage_id: int
) -> dict[TeamLabel, list[Registration]]:
    teams = (
        await db_session.execute(select(StageTeam).where(StageTeam.stage_id == stage_id))
    ).scalars().all()
    result: dict[TeamLabel, list[Registration]] = {TeamLabel.A: [], TeamLabel.B: []}
    for team in teams:
        result[team.team_label] = await get_stage_team_members(db_session, team.id)
    return result


EDITABLE_TEAM_STAGE_STATUSES = (StageStatus.TEAMS_FORMED, StageStatus.CODE_SENT)


async def get_stage_outside_match_members(
    db_session: AsyncSession,
    stage_id: int,
) -> list[Registration]:
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage or not stage.group_id:
        return []

    group_members = await get_group_members(db_session, stage.group_id)
    teams = await get_stage_teams_with_members(db_session, stage_id)
    in_match_ids = {
        member.id
        for members in teams.values()
        for member in members
    }
    return [member for member in group_members if member.id not in in_match_ids]


async def add_player_to_stage_team(
    db_session: AsyncSession,
    stage_id: int,
    registration_id: int,
    target_team: TeamLabel,
) -> None:
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        raise ValueError("Этап не найден.")
    if stage.status not in EDITABLE_TEAM_STAGE_STATUSES:
        raise ValueError("Добавление в команду доступно только в активном раунде до внесения результатов.")

    if not stage.group_id:
        raise ValueError("Этап не привязан к группе.")

    await prune_stage_team_roster(db_session, stage_id)

    group_members = await get_group_members(db_session, stage.group_id)
    if not any(member.id == registration_id for member in group_members):
        raise ValueError("Участник не состоит в этой группе.")

    teams = await get_stage_teams_with_members(db_session, stage_id)
    in_match_ids = {
        member.id
        for members in teams.values()
        for member in members
    }
    if registration_id in in_match_ids:
        raise ValueError("Участник уже в одной из команд этого матча.")

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == stage.tournament_id))
    ).scalar_one_or_none()
    subgroup_size = resolve_subgroup_size(tour)

    stage_teams = (
        await db_session.execute(select(StageTeam).where(StageTeam.stage_id == stage_id))
    ).scalars().all()
    team_by_label = {team.team_label: team for team in stage_teams}
    if target_team not in team_by_label:
        raise ValueError("Целевая команда не найдена.")

    teams = await get_stage_teams_with_members(db_session, stage_id)
    current_count = len(teams[target_team])
    if current_count >= subgroup_size:
        raise ValueError(
            f"В команде {target_team.value} уже {current_count} из {subgroup_size} игроков."
        )

    db_session.add(
        StageTeamMember(
            stage_team_id=team_by_label[target_team].id,
            registration_id=registration_id,
        )
    )


async def assign_player_to_stage_team(
    db_session: AsyncSession,
    bot: Bot,
    stage_id: int,
    registration_id: int,
    target_team: TeamLabel,
    admin_id: int | None = None,
    *,
    send_notifications: bool = True,
) -> Registration:
    from bot.services.replacements import add_member_to_group

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage or not stage.group_id:
        raise ValueError("Этап не найден.")

    group_member_ids = {
        member.id for member in await get_group_members(db_session, stage.group_id)
    }
    if registration_id not in group_member_ids:
        await add_member_to_group(
            db_session,
            bot,
            stage.tournament_id,
            stage.group_id,
            registration_id,
            admin_id=admin_id,
            send_notifications=send_notifications,
        )
    await add_player_to_stage_team(db_session, stage_id, registration_id, target_team)
    reg = (
        await db_session.execute(
            select(Registration).where(Registration.id == registration_id)
        )
    ).scalar_one()
    return reg


async def move_player_between_teams(
    db_session: AsyncSession,
    stage_id: int,
    registration_id: int,
    target_team: TeamLabel,
) -> None:
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        raise ValueError("Этап не найден.")
    if stage.status not in EDITABLE_TEAM_STAGE_STATUSES:
        raise ValueError("Редактирование команд доступно только в активном раунде до внесения результатов.")

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == stage.tournament_id))
    ).scalar_one_or_none()
    subgroup_size = resolve_subgroup_size(tour)

    teams = (
        await db_session.execute(select(StageTeam).where(StageTeam.stage_id == stage_id))
    ).scalars().all()
    team_by_label = {team.team_label: team for team in teams}
    if target_team not in team_by_label:
        raise ValueError("Целевая команда не найдена.")

    current_member = (
        await db_session.execute(
            select(StageTeamMember)
            .join(StageTeam, StageTeam.id == StageTeamMember.stage_team_id)
            .where(
                StageTeam.stage_id == stage_id,
                StageTeamMember.registration_id == registration_id,
            )
        )
    ).scalar_one_or_none()
    if not current_member:
        raise ValueError("Участник не найден в командах этого этапа.")

    current_team = (
        await db_session.execute(
            select(StageTeam).where(StageTeam.id == current_member.stage_team_id)
        )
    ).scalar_one()
    if current_team.team_label == target_team:
        raise ValueError("Участник уже в этой команде.")

    roster = await get_stage_teams_with_members(db_session, stage_id)
    target_count = len(roster[target_team])
    if target_count >= subgroup_size:
        raise ValueError(
            f"В команде {target_team.value} уже {target_count} из {subgroup_size} игроков."
        )

    await db_session.delete(current_member)
    db_session.add(
        StageTeamMember(
            stage_team_id=team_by_label[target_team].id,
            registration_id=registration_id,
        )
    )


async def format_stage_teams_text(db_session: AsyncSession, stage: Stage) -> str:
    from db.models import TournamentGroup

    group_number = "?"
    if stage.group_id:
        group_obj = (
            await db_session.execute(
                select(TournamentGroup).where(TournamentGroup.id == stage.group_id)
            )
        ).scalar_one_or_none()
        if group_obj:
            group_number = group_obj.group_number

    groups = await get_tournament_groups(db_session, stage.tournament_id)
    cycle = cycle_for_stage_number(stage.stage_number, len(groups))
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == stage.tournament_id))
    ).scalar_one_or_none()
    subgroup_size = resolve_subgroup_size(tour)
    teams = await get_stage_teams_with_members(db_session, stage.id)
    outside = await get_stage_outside_match_members(db_session, stage.id)

    lines = [
        f"🎮 Матч группы {group_number}, круг {cycle} (этап #{stage.stage_number})",
        f"Статус: {stage.status.value}",
    ]
    if stage.match_code:
        lines.append(f"Код лобби: {stage.match_code}")

    for label in (TeamLabel.A, TeamLabel.B):
        count = len(teams[label])
        lines.append(f"\nКоманда {label.value} ({count}/{subgroup_size}):")
        if teams[label]:
            for member in teams[label]:
                lines.append(f"  • {public_player_label(member)}")
        else:
            lines.append("  —")

    if outside:
        lines.append("\nВне матча:")
        for member in outside:
            lines.append(f"  • {public_player_label(member)}")
    return "\n".join(lines)


async def format_tournament_rounds_text(
    db_session: AsyncSession,
    tour_id: int,
    *,
    admin_view: bool = False,
) -> str:
    stages = await get_tournament_stages(db_session, tour_id)
    groups = await get_tournament_groups(db_session, tour_id)
    num_groups = len(groups)
    group_map = {group.id: group.group_number for group in groups}

    formed_stages = [
        stage for stage in stages
        if stage.status in (
            StageStatus.TEAMS_FORMED,
            StageStatus.CODE_SENT,
            StageStatus.COMPLETED,
        )
    ]
    if not formed_stages:
        return "🎮 Составы команд по раундам появятся после формирования команд."

    lines = ["🎮 Составы команд по раундам:\n"]
    for stage in formed_stages:
        cycle = cycle_for_stage_number(stage.stage_number, num_groups) if num_groups else 1
        group_number = group_map.get(stage.group_id, "?")
        lines.append(f"Раунд {cycle}, группа {group_number} (этап #{stage.stage_number}):")
        teams = await get_stage_teams_with_members(db_session, stage.id)
        for label in (TeamLabel.A, TeamLabel.B):
            lines.append(f"  Команда {label.value}:")
            for member in teams[label]:
                nick = admin_player_label(member) if admin_view else public_player_label(member)
                lines.append(f"    • {nick}")
        lines.append("")
    return "\n".join(lines).strip()


async def format_group_rounds_text(
    db_session: AsyncSession,
    tour_id: int,
    group_id: int,
    *,
    admin_view: bool = False,
) -> str:
    groups = await get_tournament_groups(db_session, tour_id)
    num_groups = len(groups)
    group = next((item for item in groups if item.id == group_id), None)
    if not group:
        return "❌ Группа не найдена."

    stages = [
        stage for stage in await get_tournament_stages(db_session, tour_id)
        if not stage.is_final
        and stage.group_id == group_id
        and stage.status in (
            StageStatus.TEAMS_FORMED,
            StageStatus.CODE_SENT,
            StageStatus.COMPLETED,
        )
    ]
    if not stages:
        return "🎮 Составы команд по раундам для этой группы появятся после формирования команд."

    lines = [f"🎮 Раунды группы {group.group_number}:\n"]
    for stage in stages:
        cycle = cycle_for_stage_number(stage.stage_number, num_groups) if num_groups else 1
        lines.append(f"Раунд {cycle} (этап #{stage.stage_number}):")
        teams = await get_stage_teams_with_members(db_session, stage.id)
        for label in (TeamLabel.A, TeamLabel.B):
            lines.append(f"  Команда {label.value}:")
            for member in teams[label]:
                nick = admin_player_label(member) if admin_view else public_player_label(member)
                lines.append(f"    • {nick}")
        lines.append("")
    return "\n".join(lines).strip()


async def get_group_stage_progress(db_session: AsyncSession, tour_id: int) -> dict[str, Any]:
    groups = await get_tournament_groups(db_session, tour_id)
    num_groups = len(groups)
    if num_groups == 0:
        return {"state": "no_groups"}

    stages = [stage for stage in await get_tournament_stages(db_session, tour_id) if not stage.is_final]
    meta = await get_tournament_meta(db_session, tour_id)
    completed = [stage for stage in stages if stage.status == StageStatus.COMPLETED]
    active = next(
        (stage for stage in stages if stage.status != StageStatus.COMPLETED),
        None,
    )
    approved_cycles = get_approved_cycles(meta)
    group_stage_finished = bool(meta.get("group_stage_finished"))
    finished_cycles = completed_cycles_count(completed, num_groups)

    if group_stage_finished:
        return {"state": "finished", "groups": groups, "stages": stages, "completed": completed}

    if active:
        group = next((g for g in groups if g.id == active.group_id), None)
        return {
            "state": "active_stage",
            "stage": active,
            "group": group,
            "groups": groups,
            "stages": stages,
        }

    if num_groups > 0 and finished_cycles > 0 and finished_cycles == approved_cycles:
        return {
            "state": "cycle_decision",
            "groups": groups,
            "stages": stages,
            "completed": completed,
            "finished_cycles": finished_cycles,
        }

    next_stage_number = len(stages) + 1
    next_cycle = cycle_for_stage_number(next_stage_number, num_groups)
    if next_cycle > approved_cycles:
        return {
            "state": "cycle_decision",
            "groups": groups,
            "stages": stages,
            "completed": completed,
            "finished_cycles": finished_cycles,
        }

    group = groups[group_index_for_stage_number(next_stage_number, num_groups)]
    unconfirmed = [
        member for member in await get_group_members(db_session, group.id)
        if not member.participation_confirmed
    ]
    if unconfirmed:
        return {
            "state": "awaiting_replacement_confirm",
            "group": group,
            "cycle": next_cycle,
            "unconfirmed": unconfirmed,
            "groups": groups,
        }

    return {
        "state": "ready_to_form",
        "group": group,
        "cycle": next_cycle,
        "stage_number": next_stage_number,
        "groups": groups,
    }


async def create_stage_with_random_teams(
    db_session: AsyncSession, tour: Tournament, group_id: int, stage_number: int
) -> Stage:
    members = await get_group_members(db_session, group_id)
    subgroup_size = tour.subgroup_size or 5
    expected = tour.group_size or 10
    if len(members) != expected:
        raise ValueError(f"В группе должно быть {expected} участников, сейчас {len(members)}.")

    unconfirmed = [member for member in members if not member.participation_confirmed]
    if unconfirmed:
        names = ", ".join(public_player_label(member) for member in unconfirmed)
        raise ValueError(f"Не все участники группы подтвердили участие: {names}")

    stage = Stage(
        tournament_id=tour.id,
        group_id=group_id,
        stage_number=stage_number,
        status=StageStatus.PENDING,
        is_final=False,
    )
    db_session.add(stage)
    await db_session.flush()

    shuffled = list(members)
    secrets.SystemRandom().shuffle(shuffled)
    team_a = shuffled[:subgroup_size]
    team_b = shuffled[subgroup_size:expected]

    for label, chunk in ((TeamLabel.A, team_a), (TeamLabel.B, team_b)):
        stage_team = StageTeam(stage_id=stage.id, team_label=label)
        db_session.add(stage_team)
        await db_session.flush()
        for member in chunk:
            db_session.add(
                StageTeamMember(stage_team_id=stage_team.id, registration_id=member.id)
            )

    stage.status = StageStatus.TEAMS_FORMED
    if tour.status == TournamentStatus.GROUPS_FORMED:
        tour.status = TournamentStatus.STAGE_IN_PROGRESS
    return stage


LOBBY_CODE_COPY_HINT = " (код копируется при нажатии)"


async def format_lobby_code_message_for_player(
    db_session: AsyncSession,
    stage: Stage,
    registration_id: int,
    *,
    message_prefix: str = "🎮 Код лобби для вашего матча",
) -> str:
    """
    Текст DM с кодом лобби и полным списком команд этапа.
    Для игрока в составе помечает его команду.
    """
    teams = await get_stage_teams_with_members(db_session, stage.id)
    player_team: TeamLabel | None = None
    for label, members in teams.items():
        if any(member.id == registration_id for member in members):
            player_team = label
            break

    lines = [
        f"{message_prefix}:\n",
        f"<code>{stage.match_code}</code>{LOBBY_CODE_COPY_HINT}",
        "\n👥 Состав команд:",
    ]
    for label in (TeamLabel.A, TeamLabel.B):
        team_marker = " ← ваша команда" if label == player_team else ""
        lines.append(f"\nКоманда {label.value}{team_marker}:")
        if teams[label]:
            for member in teams[label]:
                lines.append(f"  • {public_player_label(member)}")
        else:
            lines.append("  —")
    lines.append("\nУдачи на раунде!")
    return "\n".join(lines)


async def assign_and_send_match_code(
    db_session: AsyncSession, bot, stage_id: int, code: str
) -> tuple[int, int]:
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        raise ValueError("Этап не найден.")
    if stage.status not in (StageStatus.TEAMS_FORMED, StageStatus.PENDING):
        raise ValueError("Сначала нужно сформировать команды для этого раунда.")

    stage.match_code = code.strip()
    stage.code_sent_at = now_moscow()
    stage.status = StageStatus.CODE_SENT
    return await resend_match_code_to_group(db_session, bot, stage_id)


async def resend_match_code_to_group(
    db_session: AsyncSession,
    bot,
    stage_id: int,
) -> tuple[int, int]:
    """
    Рассылает код лобби всем участникам группы/финала.
    Каждый получает код и полный список команд с пометкой своей.
    """
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage or not stage.match_code:
        raise ValueError("Код лобби для этого раунда ещё не задан.")
    if stage.status not in (StageStatus.CODE_SENT, StageStatus.TEAMS_FORMED):
        raise ValueError("Повторная отправка кода доступна только в активном раунде.")

    if stage.is_final or not stage.group_id:
        members = [
            member for chunk in (await get_stage_teams_with_members(db_session, stage_id)).values()
            for member in chunk
        ]
        message_prefix = "🏆 Код лобби финала"
    else:
        members = await get_group_members(db_session, stage.group_id)
        message_prefix = "🎮 Код лобби для вашего матча"
    sent = 0
    failed = 0
    for member in members:
        user = (
            await db_session.execute(select(User).where(User.id == member.user_id))
        ).scalar_one_or_none()
        if not user:
            failed += 1
            continue
        try:
            text = await format_lobby_code_message_for_player(
                db_session,
                stage,
                member.id,
                message_prefix=message_prefix,
            )
            await bot.send_message(
                user.telegram_id,
                text,
                parse_mode="HTML",
            )
            sent += 1
        except Exception:
            failed += 1

    if stage.status == StageStatus.TEAMS_FORMED:
        stage.status = StageStatus.CODE_SENT
        stage.code_sent_at = now_moscow()
    return sent, failed


async def complete_stage(db_session: AsyncSession, stage_id: int) -> None:
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        return
    stage.status = StageStatus.COMPLETED
    stage.played_at = now_moscow()


async def finish_group_stage(db_session: AsyncSession, tour_id: int) -> None:
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        return
    meta = await get_tournament_meta(db_session, tour_id)
    meta["group_stage_finished"] = True
    await save_tournament_meta(db_session, tour_id, meta)
    tour.status = TournamentStatus.RATING_CALCULATED


async def approve_next_cycle(db_session: AsyncSession, tour_id: int) -> int:
    groups = await get_tournament_groups(db_session, tour_id)
    num_groups = len(groups)
    completed_count = await db_session.scalar(
        select(func.count(Stage.id)).where(
            Stage.tournament_id == tour_id,
            Stage.status == StageStatus.COMPLETED,
        )
    ) or 0
    finished_cycles = completed_count // num_groups if num_groups else 0

    meta = await get_tournament_meta(db_session, tour_id)
    meta["approved_cycles"] = finished_cycles + 1
    meta.pop("second_cycle_approved", None)
    await save_tournament_meta(db_session, tour_id, meta)
    return meta["approved_cycles"]


async def ensure_group_stage_started(db_session: AsyncSession, tour_id: int) -> None:
    meta = await get_tournament_meta(db_session, tour_id)
    if "approved_cycles" not in meta:
        meta["approved_cycles"] = 1
        await save_tournament_meta(db_session, tour_id, meta)


# Совместимость со старым именем
async def approve_second_cycle(db_session: AsyncSession, tour_id: int) -> None:
    await approve_next_cycle(db_session, tour_id)


async def format_stage_history_text(db_session: AsyncSession, tour_id: int) -> str:
    groups = await get_tournament_groups(db_session, tour_id)
    num_groups = len(groups)
    stages = await get_tournament_stages(db_session, tour_id)
    if not stages:
        return "История матчей пока пуста."

    group_map = {group.id: group.group_number for group in groups}
    lines = ["📜 История групповых матчей:\n"]
    for stage in stages:
        cycle = cycle_for_stage_number(stage.stage_number, num_groups) if num_groups else 1
        group_number = group_map.get(stage.group_id, "?")
        lines.append(
            f"• Этап #{stage.stage_number}: группа {group_number}, круг {cycle} — {stage.status.value}"
        )
    return "\n".join(lines)


async def maybe_auto_finish_group_stage(db_session: AsyncSession, tour_id: int) -> bool:
    return False
