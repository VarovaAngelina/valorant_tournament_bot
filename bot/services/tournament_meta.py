import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from db.models import TournamentSetting


async def get_tournament_meta(db_session: AsyncSession, tour_id: int) -> dict[str, Any]:
    settings = (
        await db_session.execute(
            select(TournamentSetting).where(TournamentSetting.tournament_id == tour_id)
        )
    ).scalar_one_or_none()
    if not settings or not settings.tiebreaker_order:
        return {}
    raw = settings.tiebreaker_order
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}


async def save_tournament_meta(db_session: AsyncSession, tour_id: int, meta: dict[str, Any]) -> None:
    settings = (
        await db_session.execute(
            select(TournamentSetting).where(TournamentSetting.tournament_id == tour_id)
        )
    ).scalar_one_or_none()
    if not settings:
        settings = TournamentSetting(tournament_id=tour_id)
        db_session.add(settings)
    settings.tiebreaker_order = json.dumps(meta, ensure_ascii=False)
