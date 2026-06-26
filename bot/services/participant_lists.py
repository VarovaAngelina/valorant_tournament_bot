"""Форматирование списков участников для админ-панели."""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Registration, RegistrationStatus

ACTIVE_REGISTRATION_STATUSES = (
    RegistrationStatus.REGISTERED,
    RegistrationStatus.SELECTED_MAIN,
    RegistrationStatus.SELECTED_RESERVE,
)

OUTSIDE_ROSTER_STATUSES = (
    RegistrationStatus.NOT_SELECTED,
    RegistrationStatus.EXCLUDED,
    RegistrationStatus.WITHDRAWN,
)


async def count_active_registrations(db_session: AsyncSession, tour_id: int) -> int:
    return (
        await db_session.scalar(
            select(func.count(Registration.id)).where(
                Registration.tournament_id == tour_id,
                Registration.status.in_(ACTIVE_REGISTRATION_STATUSES),
            )
        )
    ) or 0


def format_participant_lists_text(
    registrations: list[Registration],
    tour_id: int,
    *,
    points_by_reg_id: dict[int, Decimal] | None = None,
) -> str:
    """Списки по статусам; при переданном словаре добавляет баллы группового этапа."""

    def _points_note(reg_id: int) -> str:
        if points_by_reg_id is None:
            return ""
        points = points_by_reg_id.get(reg_id, Decimal("0"))
        return f" · {points} б."

    main_list = [r for r in registrations if r.status == RegistrationStatus.SELECTED_MAIN]
    reserve_list = [r for r in registrations if r.status == RegistrationStatus.SELECTED_RESERVE]
    queue_list = [r for r in registrations if r.status == RegistrationStatus.REGISTERED]
    outside_list = [r for r in registrations if r.status in OUTSIDE_ROSTER_STATUSES]

    text = f"📋 Списки участников турнира (ID: {tour_id}):\n"
    if points_by_reg_id is not None:
        text += "Баллы указаны за групповой этап.\n"
    text += "\n"

    text += f"🟢 Основной состав ({len(main_list)}):\n"
    for r in main_list:
        conf = "✅" if r.participation_confirmed else "⏳"
        text += f"- {r.contact_telegram} ({r.game_nick}) {conf}{_points_note(r.id)}\n"
    if not main_list:
        text += "—\n"

    text += f"\n🔵 Резерв ({len(reserve_list)}):\n"
    for r in reserve_list:
        text += f"- {r.contact_telegram} ({r.game_nick}){_points_note(r.id)}\n"
    if not reserve_list:
        text += "—\n"

    text += f"\n⏳ Очередь / подача заявок ({len(queue_list)}):\n"
    for r in queue_list:
        text += f"- {r.contact_telegram} ({r.game_nick}){_points_note(r.id)}\n"
    if not queue_list:
        text += "—\n"

    text += f"\n⚪ Вне матча ({len(outside_list)}):\n"
    for r in outside_list:
        reason = f" — {r.exclusion_reason}" if r.exclusion_reason else ""
        text += f"- {r.contact_telegram} ({r.game_nick}){reason}{_points_note(r.id)}\n"
    if not outside_list:
        text += "—\n"

    return text.rstrip()
