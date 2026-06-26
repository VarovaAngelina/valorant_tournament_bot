from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from bot.keyboards.menu import get_user_inline_menu
from bot.services.rules import get_global_rules_url
from bot.services.tournament_helpers import PARTICIPANT_STATUSES, USER_LIVE_TOURNAMENT_STATUSES, get_latest_tournament
from db.models import Registration, Tournament, TournamentStatus, User

ACTIVE_TOURNAMENT_STATUSES = (
    TournamentStatus.REGISTRATION_OPEN,
    TournamentStatus.REGISTRATION_CLOSED,
    TournamentStatus.SELECTION_DONE,
    TournamentStatus.CONFIRMATION_PENDING,
    TournamentStatus.GROUPS_FORMED,
    TournamentStatus.STAGE_IN_PROGRESS,
    TournamentStatus.RATING_CALCULATED,
    TournamentStatus.FINALISTS_SELECTED,
    TournamentStatus.FINAL_IN_PROGRESS,
    TournamentStatus.COMPLETED,
)


async def get_user_menu_context(
    db_session: AsyncSession,
    telegram_id: int,
) -> tuple[bool, bool, bool, bool]:
    reg = (
        await db_session.execute(
            select(Registration)
            .join(User)
            .join(Tournament, Tournament.id == Registration.tournament_id)
            .where(
                User.telegram_id == telegram_id,
                Registration.status.in_(PARTICIPANT_STATUSES),
                Tournament.status.in_(ACTIVE_TOURNAMENT_STATUSES),
            )
            .order_by(Registration.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if reg:
        active_tour = (
            await db_session.execute(
                select(Tournament).where(Tournament.id == reg.tournament_id)
            )
        ).scalar_one_or_none()
        is_participant = True
    else:
        active_tour = await get_latest_tournament(db_session, ACTIVE_TOURNAMENT_STATUSES)
        is_participant = False

    tournament_active = (
        active_tour is not None and active_tour.status in USER_LIVE_TOURNAMENT_STATUSES
    )
    can_edit_profile = (
        is_participant
        and active_tour is not None
        and active_tour.status in (
            TournamentStatus.REGISTRATION_OPEN,
            TournamentStatus.REGISTRATION_CLOSED,
            TournamentStatus.CONFIRMATION_PENDING,
        )
    )

    can_register = False
    open_tour = await get_latest_tournament(
        db_session,
        (TournamentStatus.REGISTRATION_OPEN,),
    )
    if open_tour:
        has_registration = (
            await db_session.execute(
                select(Registration.id)
                .join(User)
                .where(
                    User.telegram_id == telegram_id,
                    Registration.tournament_id == open_tour.id,
                    Registration.status.in_(PARTICIPANT_STATUSES),
                )
                .limit(1)
            )
        ).scalar_one_or_none() is not None
        can_register = not has_registration

    return is_participant, tournament_active, can_edit_profile, can_register


async def build_user_inline_menu(
    db_session: AsyncSession,
    telegram_id: int,
    role: str,
    state: FSMContext,
):
    state_data = await state.get_data()
    admin_mode = state_data.get("admin_mode", False)
    (
        is_participant,
        tournament_active,
        can_edit_profile,
        can_register,
    ) = await get_user_menu_context(db_session, telegram_id)
    rules_url = await get_global_rules_url(db_session)
    return get_user_inline_menu(
        role=role,
        is_participant=is_participant,
        tournament_active=tournament_active,
        can_edit_profile=can_edit_profile,
        can_register=can_register,
        admin_mode=admin_mode,
        rules_url=rules_url,
    )
