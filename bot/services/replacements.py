from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from bot.services.grouping import get_group_member_count, resolve_group_capacity
from bot.services.participant_lists import OUTSIDE_ROSTER_STATUSES
from bot.utils.timezone import now_moscow
from db.models import (
    GroupMember,
    Registration,
    RegistrationStatus,
    ReplacementLog,
    Stage,
    StageResult,
    StageStatus,
    StageTeamMember,
    StageTeam,
    Tournament,
    TournamentGroup,
    User,
)


OUTSIDE_MATCH_REASON = "Вне матча"


def decline_participation_confirm_keyboard(registration_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, отказаться",
                    callback_data=f"decline_participation_confirm_{registration_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"decline_participation_cancel_{registration_id}",
                ),
            ]
        ]
    )


def confirm_participation_keyboard(registration_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтверждаю участие",
                    callback_data=f"confirm_participation_{registration_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отказаться от участия",
                    callback_data=f"decline_participation_{registration_id}",
                ),
            ]
        ]
    )


async def get_available_reserves(db_session: AsyncSession, tour_id: int) -> list[Registration]:
    result = await db_session.execute(
        select(Registration).where(
            Registration.tournament_id == tour_id,
            Registration.status == RegistrationStatus.SELECTED_RESERVE,
        ).order_by(Registration.id)
    )
    return list(result.scalars().all())


async def get_replaced_out_candidates(
    db_session: AsyncSession,
    tour_id: int,
) -> list[Registration]:
    result = await db_session.execute(
        select(Registration)
        .join(ReplacementLog, ReplacementLog.old_registration_id == Registration.id)
        .where(
            Registration.tournament_id == tour_id,
            Registration.status == RegistrationStatus.EXCLUDED,
        )
        .order_by(ReplacementLog.replaced_at.desc(), Registration.id)
    )
    return list(result.scalars().all())


async def get_outside_roster_candidates(
    db_session: AsyncSession,
    tour_id: int,
) -> list[Registration]:
    in_group_subq = select(GroupMember.registration_id)
    result = await db_session.execute(
        select(Registration).where(
            Registration.tournament_id == tour_id,
            Registration.status.in_(OUTSIDE_ROSTER_STATUSES),
            ~Registration.id.in_(in_group_subq),
        ).order_by(Registration.id)
    )
    return list(result.scalars().all())


async def get_swap_candidates(
    db_session: AsyncSession,
    tour_id: int,
    group_id: int,
) -> list[Registration]:
    current_member_ids = (
        await db_session.execute(
            select(GroupMember.registration_id).where(GroupMember.group_id == group_id)
        )
    ).scalars().all()

    result = await db_session.execute(
        select(Registration)
        .join(GroupMember, GroupMember.registration_id == Registration.id)
        .where(
            Registration.tournament_id == tour_id,
            Registration.status == RegistrationStatus.SELECTED_MAIN,
            GroupMember.group_id != group_id,
            Registration.participation_confirmed.is_(True),
        )
        .order_by(Registration.id)
    )
    return list(result.scalars().all())


async def get_replaced_players(db_session: AsyncSession, tour_id: int) -> list[tuple[Registration, Registration]]:
    logs = (
        await db_session.execute(
            select(ReplacementLog)
            .where(ReplacementLog.tournament_id == tour_id)
            .order_by(ReplacementLog.replaced_at.desc())
        )
    ).scalars().all()
    pairs: list[tuple[Registration, Registration]] = []
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
        if old_reg and new_reg:
            pairs.append((old_reg, new_reg))
    return pairs


async def get_group_replacements(
    db_session: AsyncSession,
    tour_id: int,
    group_id: int,
) -> list[tuple[Registration, Registration]]:
    member_ids = set(
        (
            await db_session.execute(
                select(GroupMember.registration_id).where(GroupMember.group_id == group_id)
            )
        ).scalars().all()
    )
    pairs = []
    for old_reg, new_reg in await get_replaced_players(db_session, tour_id):
        if new_reg.id in member_ids:
            pairs.append((old_reg, new_reg))
    return pairs


async def _remove_from_active_stage_teams(
    db_session: AsyncSession,
    group_id: int,
    registration_id: int,
) -> None:
    active_stages = (
        await db_session.execute(
            select(Stage).where(
                Stage.group_id == group_id,
                Stage.status.in_([StageStatus.TEAMS_FORMED, StageStatus.CODE_SENT]),
            )
        )
    ).scalars().all()
    for stage in active_stages:
        team_members = (
            await db_session.execute(
                select(StageTeamMember)
                .join(StageTeam, StageTeam.id == StageTeamMember.stage_team_id)
                .where(
                    StageTeam.stage_id == stage.id,
                    StageTeamMember.registration_id == registration_id,
                )
            )
        ).scalars().all()
        for team_member in team_members:
            await db_session.delete(team_member)


async def remove_group_member(
    db_session: AsyncSession,
    tour_id: int,
    group_id: int,
    registration_id: int,
    *,
    reason: str = "Исключён из группы",
) -> Registration:
    old_reg = (
        await db_session.execute(
            select(Registration).where(
                Registration.id == registration_id,
                Registration.tournament_id == tour_id,
            )
        )
    ).scalar_one_or_none()
    if not old_reg:
        raise ValueError("Участник не найден.")

    group_member = (
        await db_session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id,
                GroupMember.registration_id == registration_id,
            )
        )
    ).scalar_one_or_none()
    if not group_member:
        raise ValueError("Участник не найден в этой группе.")

    await db_session.delete(group_member)
    await _remove_from_active_stage_teams(db_session, group_id, registration_id)

    old_reg.status = RegistrationStatus.EXCLUDED
    old_reg.exclusion_reason = reason
    old_reg.excluded_at = now_moscow()
    old_reg.participation_confirmed = False
    old_reg.participation_confirmed_at = None
    return old_reg


async def add_member_to_group(
    db_session: AsyncSession,
    bot: Bot,
    tour_id: int,
    group_id: int,
    registration_id: int,
    admin_id: int | None = None,
    *,
    send_notifications: bool = True,
    auto_confirm: bool = False,
) -> Registration:
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        raise ValueError("Турнир не найден.")

    group = (
        await db_session.execute(
            select(TournamentGroup).where(
                TournamentGroup.id == group_id,
                TournamentGroup.tournament_id == tour_id,
            )
        )
    ).scalar_one_or_none()
    if not group:
        raise ValueError("Группа не найдена.")

    group_size = resolve_group_capacity(tour)
    member_count = await get_group_member_count(db_session, group_id)
    if member_count >= group_size:
        raise ValueError(f"В группе {group.group_number} уже {group_size} участников.")

    reg = (
        await db_session.execute(
            select(Registration).where(
                Registration.id == registration_id,
                Registration.tournament_id == tour_id,
            )
        )
    ).scalar_one_or_none()
    if not reg:
        raise ValueError("Участник не найден.")

    if reg.status not in (
        RegistrationStatus.SELECTED_RESERVE,
        RegistrationStatus.SELECTED_MAIN,
        *OUTSIDE_ROSTER_STATUSES,
    ):
        raise ValueError(
            "Можно добавить только резервиста, участника из списка «Вне матча» или нового игрока."
        )

    existing_member = (
        await db_session.execute(
            select(GroupMember).where(GroupMember.registration_id == registration_id)
        )
    ).scalar_one_or_none()
    if existing_member:
        if existing_member.group_id == group_id:
            raise ValueError("Участник уже в этой группе.")
        raise ValueError("Участник уже состоит в другой группе.")

    db_session.add(GroupMember(group_id=group_id, registration_id=registration_id))
    if reg.status in OUTSIDE_ROSTER_STATUSES:
        reg.exclusion_reason = None
        reg.excluded_at = None
    reg.status = RegistrationStatus.SELECTED_MAIN
    if auto_confirm:
        reg.participation_confirmed = True
        reg.participation_confirmed_at = now_moscow()
    else:
        reg.participation_confirmed = False
        reg.participation_confirmed_at = None
        if send_notifications:
            await send_participation_request(db_session, bot, reg.id)
    return reg


async def _sync_active_stage_teams(
    db_session: AsyncSession,
    group_id: int,
    old_registration_id: int,
    new_registration_id: int,
) -> None:
    active_stages = (
        await db_session.execute(
            select(Stage).where(
                Stage.group_id == group_id,
                Stage.status.in_([StageStatus.TEAMS_FORMED, StageStatus.CODE_SENT]),
            )
        )
    ).scalars().all()
    for stage in active_stages:
        team_members = (
            await db_session.execute(
                select(StageTeamMember)
                .join(StageTeam, StageTeam.id == StageTeamMember.stage_team_id)
                .where(
                    StageTeam.stage_id == stage.id,
                    StageTeamMember.registration_id == old_registration_id,
                )
            )
        ).scalars().all()
        for team_member in team_members:
            team_member.registration_id = new_registration_id


async def replace_group_member(
    db_session: AsyncSession,
    bot: Bot,
    tour_id: int,
    group_id: int,
    old_registration_id: int,
    new_registration_id: int,
    admin_id: int | None = None,
    *,
    send_notifications: bool = True,
) -> Registration:
    old_reg = (
        await db_session.execute(
            select(Registration).where(Registration.id == old_registration_id)
        )
    ).scalar_one_or_none()
    new_reg = (
        await db_session.execute(
            select(Registration).where(Registration.id == new_registration_id)
        )
    ).scalar_one_or_none()
    if not old_reg or not new_reg:
        raise ValueError("Участник или замена не найдены.")

    if new_reg.status not in (
        RegistrationStatus.SELECTED_RESERVE,
        RegistrationStatus.SELECTED_MAIN,
        *OUTSIDE_ROSTER_STATUSES,
    ):
        raise ValueError("Выбранный игрок недоступен для замены.")

    if new_reg.status == RegistrationStatus.SELECTED_MAIN:
        source_group_member = (
            await db_session.execute(
                select(GroupMember).where(GroupMember.registration_id == new_registration_id)
            )
        ).scalar_one_or_none()
        if source_group_member:
            if source_group_member.group_id == group_id:
                raise ValueError("Нельзя заменить участника самим собой.")
            await db_session.delete(source_group_member)

    await db_session.execute(
        update(StageResult)
        .where(StageResult.registration_id == old_registration_id)
        .values(registration_id=new_registration_id)
    )

    old_group_member = (
        await db_session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id,
                GroupMember.registration_id == old_registration_id,
            )
        )
    ).scalar_one_or_none()
    if not old_group_member:
        raise ValueError("Игрок не найден в этой группе.")

    old_group_member.registration_id = new_registration_id
    if new_reg.status in OUTSIDE_ROSTER_STATUSES:
        new_reg.exclusion_reason = None
        new_reg.excluded_at = None
    old_reg.status = RegistrationStatus.EXCLUDED
    old_reg.exclusion_reason = "Заменён в группе"
    old_reg.excluded_at = now_moscow()

    new_reg.status = RegistrationStatus.SELECTED_MAIN
    new_reg.participation_confirmed = False
    new_reg.participation_confirmed_at = None

    await _sync_active_stage_teams(db_session, group_id, old_registration_id, new_registration_id)

    from db.models import Stage, StageStatus

    active_stage = (
        await db_session.execute(
            select(Stage)
            .where(
                Stage.group_id == group_id,
                Stage.status.in_([StageStatus.TEAMS_FORMED, StageStatus.CODE_SENT, StageStatus.RESULT_ENTERED]),
            )
            .order_by(Stage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    db_session.add(
        ReplacementLog(
            tournament_id=tour_id,
            old_registration_id=old_registration_id,
            new_registration_id=new_registration_id,
            from_stage_id=active_stage.id if active_stage else None,
            replaced_by_admin_id=admin_id,
        )
    )

    if send_notifications:
        await send_participation_request(db_session, bot, new_reg.id)
    return new_reg


async def send_participation_request(
    db_session: AsyncSession,
    bot: Bot,
    registration_id: int,
) -> bool:
    reg = (
        await db_session.execute(
            select(Registration).where(Registration.id == registration_id)
        )
    ).scalar_one_or_none()
    if not reg:
        return False
    user = (
        await db_session.execute(select(User).where(User.id == reg.user_id))
    ).scalar_one_or_none()
    if not user:
        return False
    try:
        await bot.send_message(
            user.telegram_id,
            "♻️ Вас поставили в состав группы вместо другого участника.\n"
            "Подтвердите участие кнопкой ниже.",
            reply_markup=confirm_participation_keyboard(reg.id),
        )
        return True
    except Exception:
        return False


async def send_lobby_code_to_player(
    db_session: AsyncSession,
    bot: Bot,
    stage_id: int,
    registration_id: int,
) -> bool:
    """Отправляет одному игроку код лобби и список команд этапа."""
    from bot.services.stages import format_lobby_code_message_for_player

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage or not stage.match_code:
        raise ValueError("Код лобби для этого раунда ещё не задан.")

    reg = (
        await db_session.execute(
            select(Registration).where(Registration.id == registration_id)
        )
    ).scalar_one_or_none()
    if not reg:
        raise ValueError("Участник не найден.")

    user = (
        await db_session.execute(select(User).where(User.id == reg.user_id))
    ).scalar_one_or_none()
    if not user:
        raise ValueError("Telegram-профиль участника не найден.")

    prefix = "🏆 Код лобби финала" if stage.is_final else "🎮 Код лобби для вашего матча"
    text = await format_lobby_code_message_for_player(
        db_session,
        stage,
        registration_id,
        message_prefix=prefix,
    )
    await bot.send_message(user.telegram_id, text, parse_mode="HTML")
    return True


def replacement_followup_keyboard(
    tour_id: int,
    group_id: int,
    new_registration_id: int,
    stage_id: int | None = None,
    *,
    show_dev_confirm: bool = False,
) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(
            text="📨 Отправить подтверждение",
            callback_data=f"stage_resend_confirm_{new_registration_id}",
        )
    ]]
    if show_dev_confirm:
        rows.append([
            InlineKeyboardButton(
                text="🧪 Подтвердить участие",
                callback_data=f"stage_dev_confirm_{new_registration_id}",
            )
        ])
    if stage_id:
        rows.append([
            InlineKeyboardButton(
                text="🔁 Отправить код игроку",
                callback_data=f"stage_resend_code_player_{stage_id}_{new_registration_id}",
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text="🔁 Повторно отправить код группе",
                callback_data=f"stage_resend_code_group_{stage_id}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"manage_tour_{tour_id}",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
