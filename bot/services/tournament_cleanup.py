from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    Finalist,
    GroupMember,
    MvpAward,
    NotificationsLog,
    Registration,
    ReplacementLog,
    Stage,
    StageResult,
    StageTeam,
    StageTeamMember,
    SubscriptionEvent,
    Tournament,
    TournamentGroup,
    TournamentSetting,
)


async def delete_tournament_cascade(db_session: AsyncSession, tour_id: int) -> None:
    reg_ids = (
        await db_session.execute(
            select(Registration.id).where(Registration.tournament_id == tour_id)
        )
    ).scalars().all()

    group_ids = (
        await db_session.execute(
            select(TournamentGroup.id).where(TournamentGroup.tournament_id == tour_id)
        )
    ).scalars().all()

    stage_ids = (
        await db_session.execute(
            select(Stage.id).where(Stage.tournament_id == tour_id)
        )
    ).scalars().all()

    stage_team_ids = []
    if stage_ids:
        stage_team_ids = (
            await db_session.execute(
                select(StageTeam.id).where(StageTeam.stage_id.in_(stage_ids))
            )
        ).scalars().all()

    if reg_ids:
        await db_session.execute(
            delete(SubscriptionEvent).where(SubscriptionEvent.registration_id.in_(reg_ids))
        )
        await db_session.execute(
            delete(NotificationsLog).where(NotificationsLog.registration_id.in_(reg_ids))
        )

    if stage_team_ids:
        await db_session.execute(
            delete(StageTeamMember).where(StageTeamMember.stage_team_id.in_(stage_team_ids))
        )

    if stage_ids:
        await db_session.execute(delete(StageResult).where(StageResult.stage_id.in_(stage_ids)))
        await db_session.execute(delete(StageTeam).where(StageTeam.stage_id.in_(stage_ids)))
        await db_session.execute(delete(Stage).where(Stage.id.in_(stage_ids)))

    if group_ids:
        await db_session.execute(delete(GroupMember).where(GroupMember.group_id.in_(group_ids)))
        await db_session.execute(delete(TournamentGroup).where(TournamentGroup.id.in_(group_ids)))

    await db_session.execute(delete(Finalist).where(Finalist.tournament_id == tour_id))
    await db_session.execute(delete(MvpAward).where(MvpAward.tournament_id == tour_id))
    await db_session.execute(delete(ReplacementLog).where(ReplacementLog.tournament_id == tour_id))
    await db_session.execute(delete(Registration).where(Registration.tournament_id == tour_id))
    await db_session.execute(delete(TournamentSetting).where(TournamentSetting.tournament_id == tour_id))
    await db_session.execute(delete(Tournament).where(Tournament.id == tour_id))
