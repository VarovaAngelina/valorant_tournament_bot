from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.utils.player_display import admin_player_label, public_player_label
from db.models import GroupMember, Registration, RegistrationStatus, ReplacementLog, Stage, Tournament, TournamentGroup


def resolve_group_capacity(tour: Tournament | None) -> int:
    if not tour or not tour.group_size:
        return 10
    return tour.group_size


async def get_group_member_count(db_session: AsyncSession, group_id: int) -> int:
    return len(await get_group_members(db_session, group_id))


async def format_replacement_line(
    db_session: AsyncSession,
    log: ReplacementLog,
    *,
    admin_view: bool = False,
) -> str:
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
        return ""
    old_label = admin_player_label(old_reg) if admin_view else public_player_label(old_reg)
    new_label = admin_player_label(new_reg) if admin_view else public_player_label(new_reg)
    stage_note = ""
    if log.from_stage_id:
        stage = (
            await db_session.execute(select(Stage).where(Stage.id == log.from_stage_id))
        ).scalar_one_or_none()
        if stage:
            stage_note = f" (с этапа #{stage.stage_number})"
    return f"  • {old_label} → {new_label}{stage_note}"


async def move_member_between_groups(
    db_session: AsyncSession,
    tour_id: int,
    registration_id: int,
    target_group_id: int,
) -> None:
    member = (
        await db_session.execute(
            select(GroupMember)
            .join(TournamentGroup, TournamentGroup.id == GroupMember.group_id)
            .where(
                GroupMember.registration_id == registration_id,
                TournamentGroup.tournament_id == tour_id,
            )
        )
    ).scalar_one_or_none()
    if not member:
        raise ValueError("Участник не найден в группах этого турнира.")

    target = (
        await db_session.execute(
            select(TournamentGroup).where(
                TournamentGroup.id == target_group_id,
                TournamentGroup.tournament_id == tour_id,
            )
        )
    ).scalar_one_or_none()
    if not target:
        raise ValueError("Целевая группа не найдена.")
    if member.group_id == target_group_id:
        raise ValueError("Участник уже в этой группе.")

    target_count = await get_group_member_count(db_session, target_group_id)
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    group_size = resolve_group_capacity(tour)
    if target_count >= group_size:
        raise ValueError(f"В группе {target.group_number} уже {group_size} участников.")

    member.group_id = target_group_id


async def get_main_roster(db_session: AsyncSession, tour_id: int) -> list[Registration]:
    result = await db_session.execute(
        select(Registration).where(
            Registration.tournament_id == tour_id,
            Registration.status == RegistrationStatus.SELECTED_MAIN,
        ).order_by(Registration.id)
    )
    return list(result.scalars().all())


def get_unconfirmed_players(main_players: list[Registration]) -> list[Registration]:
    return [player for player in main_players if not player.participation_confirmed]


async def get_main_roster_confirmation_stats(
    db_session: AsyncSession,
    tour_id: int,
) -> tuple[int, int]:
    main_players = await get_main_roster(db_session, tour_id)
    confirmed = sum(1 for player in main_players if player.participation_confirmed)
    return confirmed, len(main_players)


async def all_main_roster_confirmed(db_session: AsyncSession, tour_id: int) -> bool:
    confirmed, total = await get_main_roster_confirmation_stats(db_session, tour_id)
    return total > 0 and confirmed == total


async def notify_admins_all_main_roster_confirmed(
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
    if not tour or tour.status != TournamentStatus.CONFIRMATION_PENDING:
        return
    if not await all_main_roster_confirmed(db_session, tour_id):
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
                text="👥 Перейти к управлению турниром",
                callback_data=f"manage_tour_{tour_id}",
            )
        ]]
    )
    text = (
        f"✅ Все участники основного состава турнира «{tour.title}» "
        "подтвердили участие.\nМожно формировать группы."
    )
    for telegram_id in admin_ids:
        try:
            await bot.send_message(telegram_id, text, reply_markup=keyboard)
        except Exception:
            pass


async def notify_admins_player_declined_participation(
    bot,
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
                text="👥 Перейти к управлению турниром",
                callback_data=f"manage_tour_{tour_id}",
            )
        ]]
    )
    text = (
        f"❌ Участник {registration.contact_telegram} ({registration.game_nick}) "
        f"отказался от участия в турнире «{tour.title}»."
    )
    for telegram_id in admin_ids:
        try:
            await bot.send_message(telegram_id, text, reply_markup=keyboard)
        except Exception:
            pass


async def tournament_has_groups(db_session: AsyncSession, tour_id: int) -> bool:
    count = await db_session.scalar(
        select(func.count(TournamentGroup.id)).where(TournamentGroup.tournament_id == tour_id)
    )
    return bool(count)


async def format_tournament_groups_text(
    db_session: AsyncSession,
    tour_id: int,
    *,
    admin_view: bool = False,
) -> str:
    groups = (
        await db_session.execute(
            select(TournamentGroup)
            .where(TournamentGroup.tournament_id == tour_id)
            .order_by(TournamentGroup.group_number)
        )
    ).scalars().all()

    if not groups:
        return "📋 Группы для этого турнира ещё не сформированы."

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    tour_title = tour.title if tour else f"ID {tour_id}"
    group_capacity = resolve_group_capacity(tour)

    lines = [f"📋 Сформированные группы турнира «{tour_title}»:\n"]
    for group in groups:
        members = await get_group_members(db_session, group.id)

        lines.append(
            f"Группа {group.group_number} ({len(members)}/{group_capacity}):"
        )
        for member in members:
            label = admin_player_label(member) if admin_view else public_player_label(member)
            suffix = f", {member.game_rank}" if admin_view else ""
            lines.append(f"  • {label}{suffix}")

        replacements = (
            await db_session.execute(
                select(ReplacementLog).where(ReplacementLog.tournament_id == tour_id)
            )
        ).scalars().all()
        member_ids = {member.id for member in members}
        replaced_lines = []
        for log in replacements:
            if log.new_registration_id not in member_ids:
                continue
            line = await format_replacement_line(db_session, log, admin_view=admin_view)
            if line:
                replaced_lines.append(line)
        if replaced_lines:
            lines.append("  Замены:")
            lines.extend(replaced_lines)
        lines.append("")

    return "\n".join(lines).strip()


async def get_tournament_groups(db_session: AsyncSession, tour_id: int) -> list[TournamentGroup]:
    result = await db_session.execute(
        select(TournamentGroup)
        .where(TournamentGroup.tournament_id == tour_id)
        .order_by(TournamentGroup.group_number)
    )
    return list(result.scalars().all())


async def get_group_members(db_session: AsyncSession, group_id: int) -> list[Registration]:
    result = await db_session.execute(
        select(Registration)
        .join(GroupMember, GroupMember.registration_id == Registration.id)
        .where(GroupMember.group_id == group_id)
        .order_by(Registration.id)
    )
    return list(result.scalars().all())


async def format_single_group_text(
    db_session: AsyncSession,
    tour_id: int,
    group_id: int,
    *,
    admin_view: bool = False,
) -> str:
    group = (
        await db_session.execute(
            select(TournamentGroup).where(
                TournamentGroup.id == group_id,
                TournamentGroup.tournament_id == tour_id,
            )
        )
    ).scalar_one_or_none()
    if not group:
        return "❌ Группа не найдена."

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    members = await get_group_members(db_session, group_id)
    group_capacity = resolve_group_capacity(tour)
    lines = [
        f"📋 Группа {group.group_number} ({len(members)}/{group_capacity}):\n"
    ]
    for member in members:
        label = admin_player_label(member) if admin_view else public_player_label(member)
        lines.append(f"  • {label}")

    replacements = (
        await db_session.execute(
            select(ReplacementLog).where(ReplacementLog.tournament_id == tour_id)
        )
    ).scalars().all()
    member_ids = {member.id for member in members}
    replaced_lines = []
    for log in replacements:
        if log.new_registration_id not in member_ids:
            continue
        line = await format_replacement_line(db_session, log, admin_view=admin_view)
        if line:
            replaced_lines.append(line)
    if replaced_lines:
        lines.append("\n  Замены:")
        lines.extend(replaced_lines)
    return "\n".join(lines)

