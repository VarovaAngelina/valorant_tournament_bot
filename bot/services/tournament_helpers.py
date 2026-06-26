from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from db.models import Registration, RegistrationStatus, Tournament, TournamentStatus, User

USER_LIVE_TOURNAMENT_STATUSES = (
    TournamentStatus.GROUPS_FORMED,
    TournamentStatus.STAGE_IN_PROGRESS,
    TournamentStatus.RATING_CALCULATED,
    TournamentStatus.FINALISTS_SELECTED,
    TournamentStatus.FINAL_IN_PROGRESS,
)

USER_TOURNAMENT_VIEW_STATUSES = USER_LIVE_TOURNAMENT_STATUSES
USER_STATS_STATUSES = USER_LIVE_TOURNAMENT_STATUSES

PARTICIPANT_STATUSES = (
    RegistrationStatus.REGISTERED,
    RegistrationStatus.SELECTED_MAIN,
    RegistrationStatus.SELECTED_RESERVE,
)


async def get_latest_tournament(
    db_session: AsyncSession,
    statuses: tuple[TournamentStatus, ...],
) -> Tournament | None:
    return (
        await db_session.execute(
            select(Tournament)
            .where(Tournament.status.in_(statuses))
            .order_by(Tournament.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_user_registration(
    db_session: AsyncSession,
    tour_id: int,
    telegram_id: int,
) -> Registration | None:
    return (
        await db_session.execute(
            select(Registration)
            .join(User, User.id == Registration.user_id)
            .where(
                User.telegram_id == telegram_id,
                Registration.tournament_id == tour_id,
                Registration.status.in_(
                    (
                        RegistrationStatus.SELECTED_MAIN,
                        RegistrationStatus.SELECTED_RESERVE,
                        RegistrationStatus.EXCLUDED,
                    )
                ),
            )
            .order_by(Registration.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
