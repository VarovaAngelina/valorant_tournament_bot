"""Подсчёт баллов, рейтинг и форматирование статистики матчей."""

from dataclasses import dataclass
from bot.utils.timezone import now_moscow
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.stages import get_stage_teams_with_members
from bot.services.tournament_meta import get_tournament_meta, save_tournament_meta
from bot.utils.player_display import admin_player_label, public_player_label
from bot.utils.stats_format import format_acs, format_match_stats_short
from db.models import (
    Registration,
    ReplacementLog,
    Stage,
    StageResult,
    StageStatus,
    TeamLabel,
    Tournament,
    TournamentSetting,
    User,
)

TIEBREAKER_STATS = {
    "acs": "Средний ACS",
    "kills": "Сумма киллов",
    "assists": "Сумма ассистов",
    "econ_rating": "Сумма экономики",
    "first_bloods": "Первая кровь",
    "spikes_planted": "Заложено spike",
    "spikes_defused": "Обезврежено spike",
    "kda_ratio": "K/D ratio",
}

TIEBREAKER_LABELS = TIEBREAKER_STATS


@dataclass
class PlayerMatchStats:
    acs: Decimal
    kills: int
    deaths: int
    assists: int
    econ_rating: int
    first_bloods: int
    spikes_planted: int
    spikes_defused: int


def empty_player_match_stats() -> PlayerMatchStats:
    return PlayerMatchStats(
        acs=Decimal("0"),
        kills=0,
        deaths=0,
        assists=0,
        econ_rating=0,
        first_bloods=0,
        spikes_planted=0,
        spikes_defused=0,
    )


def parse_player_match_stats(text: str) -> PlayerMatchStats:
    parts = text.strip().replace(",", " ").split()
    if len(parts) != 8:
        raise ValueError(
            "Нужно 8 чисел: ACS K D A Экон FB Заложено Обезвреждено.\n"
            "Пример: 334 23 19 11 73 7 1 2"
        )
    try:
        acs = Decimal(parts[0])
        kills = int(parts[1])
        deaths = int(parts[2])
        assists = int(parts[3])
        econ_rating = int(parts[4])
        first_bloods = int(parts[5])
        spikes_planted = int(parts[6])
        spikes_defused = int(parts[7])
    except Exception as exc:
        raise ValueError("Все значения должны быть числами.") from exc

    if kills < 0 or deaths < 0 or assists < 0:
        raise ValueError("K/D/A не могут быть отрицательными.")
    return PlayerMatchStats(
        acs=acs,
        kills=kills,
        deaths=deaths,
        assists=assists,
        econ_rating=econ_rating,
        first_bloods=first_bloods,
        spikes_planted=spikes_planted,
        spikes_defused=spikes_defused,
    )


async def get_scoring_settings(db_session: AsyncSession, tour_id: int) -> tuple[int, int]:
    settings = (
        await db_session.execute(
            select(TournamentSetting).where(TournamentSetting.tournament_id == tour_id)
        )
    ).scalar_one_or_none()
    points_win = settings.points_win if settings else 4
    points_mvp = settings.points_mvp if settings else 2
    return points_win, points_mvp


async def update_scoring_settings(
    db_session: AsyncSession,
    tour_id: int,
    points_win: int,
    points_mvp: int,
) -> None:
    settings = (
        await db_session.execute(
            select(TournamentSetting).where(TournamentSetting.tournament_id == tour_id)
        )
    ).scalar_one_or_none()
    if not settings:
        settings = TournamentSetting(tournament_id=tour_id)
        db_session.add(settings)
    settings.points_win = points_win
    settings.points_mvp = points_mvp


async def recalculate_all_tournament_points(db_session: AsyncSession, tour_id: int) -> int:
    """Recalculate stored points for every stage result in the tournament."""
    stages = (
        await db_session.execute(
            select(Stage).where(
                Stage.tournament_id == tour_id,
                Stage.status.in_((StageStatus.RESULT_ENTERED, StageStatus.COMPLETED)),
            )
        )
    ).scalars().all()
    updated = 0
    for stage in stages:
        try:
            ctx = await get_stage_result_context(db_session, stage.id)
        except ValueError:
            continue
        await _recalculate_stage_result_points(
            db_session,
            stage.id,
            ctx["winning_team"],
            ctx["mvp_a_id"],
            ctx["mvp_b_id"],
        )
        updated += 1
        if stage.is_final:
            await _sync_final_awards_if_needed(db_session, stage.id)
    return updated


async def get_rating_tiebreaker(db_session: AsyncSession, tour_id: int) -> str | None:
    tiebreakers = await get_rating_tiebreakers(db_session, tour_id)
    return tiebreakers[-1] if tiebreakers else None


async def get_rating_tiebreakers(db_session: AsyncSession, tour_id: int) -> list[str]:
    meta = await get_tournament_meta(db_session, tour_id)
    if "rating_tiebreakers" in meta:
        return [stat for stat in meta["rating_tiebreakers"] if stat in TIEBREAKER_STATS]
    stat = meta.get("rating_tiebreaker")
    return [stat] if stat in TIEBREAKER_STATS else []


async def set_rating_tiebreaker(db_session: AsyncSession, tour_id: int, stat: str) -> None:
    await append_rating_tiebreaker(db_session, tour_id, stat)


async def append_rating_tiebreaker(db_session: AsyncSession, tour_id: int, stat: str) -> None:
    if stat not in TIEBREAKER_STATS:
        raise ValueError("Неизвестный параметр тай-брейка.")
    used = await get_rating_tiebreakers(db_session, tour_id)
    if stat in used:
        raise ValueError("tiebreaker_already_used")
    used.append(stat)
    meta = await get_tournament_meta(db_session, tour_id)
    meta["rating_tiebreakers"] = used
    meta.pop("rating_tiebreaker", None)
    await save_tournament_meta(db_session, tour_id, meta)


async def _player_sort_key(
    db_session: AsyncSession,
    tour_id: int,
    registration: Registration,
    points: Decimal,
    tiebreakers: list[str],
) -> tuple:
    key: list = [points]
    for stat in tiebreakers:
        key.append(
            await get_player_tiebreaker_value(db_session, tour_id, registration.id, stat)
        )
    return tuple(key)


async def aggregate_player_points(
    db_session: AsyncSession,
    tour_id: int,
    *,
    group_stage_only: bool = True,
) -> list[tuple[Registration, Decimal]]:
    query = (
        select(Registration, func.sum(StageResult.points))
        .join(StageResult, StageResult.registration_id == Registration.id)
        .join(Stage, Stage.id == StageResult.stage_id)
        .where(Stage.tournament_id == tour_id)
    )
    if group_stage_only:
        query = query.where(Stage.is_final.is_(False))
    rows = (
        await db_session.execute(
            query.group_by(Registration.id).order_by(
                func.sum(StageResult.points).desc(), Registration.id
            )
        )
    ).all()
    return [(reg, Decimal(total)) for reg, total in rows]


async def get_player_tiebreaker_value(
    db_session: AsyncSession,
    tour_id: int,
    registration_id: int,
    stat: str,
) -> Decimal:
    if stat == "acs":
        value = await db_session.scalar(
            select(func.avg(StageResult.acs))
            .join(Stage, Stage.id == StageResult.stage_id)
            .where(
                Stage.tournament_id == tour_id,
                Stage.is_final.is_(False),
                StageResult.registration_id == registration_id,
                StageResult.acs.is_not(None),
            )
        )
        return Decimal(value or 0)

    if stat == "kda_ratio":
        totals = (
            await db_session.execute(
                select(
                    func.coalesce(func.sum(StageResult.kills), 0),
                    func.coalesce(func.sum(StageResult.deaths), 0),
                )
                .join(Stage, Stage.id == StageResult.stage_id)
                .where(
                    Stage.tournament_id == tour_id,
                    Stage.is_final.is_(False),
                    StageResult.registration_id == registration_id,
                )
            )
        ).one()
        kills, deaths = totals
        if deaths == 0:
            return Decimal(kills)
        return Decimal(kills) / Decimal(deaths)

    column_map = {
        "kills": StageResult.kills,
        "assists": StageResult.assists,
        "econ_rating": StageResult.econ_rating,
        "first_bloods": StageResult.first_bloods,
        "spikes_planted": StageResult.spikes_planted,
        "spikes_defused": StageResult.spikes_defused,
    }
    column = column_map.get(stat)
    if column is None:
        return Decimal("0")

    value = await db_session.scalar(
        select(func.coalesce(func.sum(column), 0))
        .join(Stage, Stage.id == StageResult.stage_id)
        .where(
            Stage.tournament_id == tour_id,
            Stage.is_final.is_(False),
            StageResult.registration_id == registration_id,
        )
    )
    return Decimal(value or 0)


async def get_ranked_leaderboard(
    db_session: AsyncSession,
    tour_id: int,
    *,
    group_stage_only: bool = True,
) -> list[tuple[int, Registration, Decimal]]:
    base = await aggregate_player_points(db_session, tour_id, group_stage_only=group_stage_only)
    tiebreakers = await get_rating_tiebreakers(db_session, tour_id)

    enriched: list[tuple[Registration, Decimal, tuple]] = []
    for registration, total in base:
        sort_key = await _player_sort_key(
            db_session, tour_id, registration, total, tiebreakers
        )
        enriched.append((registration, total, sort_key))

    enriched.sort(key=lambda item: (item[2], -item[0].id), reverse=True)
    return [(idx + 1, reg, total) for idx, (reg, total, _) in enumerate(enriched)]


async def analyze_cutoff_tie(
    db_session: AsyncSession,
    tour_id: int,
    cutoff: int,
) -> dict:
    tiebreakers = await get_rating_tiebreakers(db_session, tour_id)
    base = await aggregate_player_points(db_session, tour_id)
    if len(base) < cutoff:
        return {"status": "not_enough_players"}

    ranked: list[tuple[Registration, Decimal, tuple]] = []
    for registration, total in base:
        sort_key = await _player_sort_key(
            db_session, tour_id, registration, total, tiebreakers
        )
        ranked.append((registration, total, sort_key))
    ranked.sort(key=lambda item: (item[2], -item[0].id), reverse=True)

    if len(ranked) <= cutoff:
        return {"status": "resolved"}

    border_key = ranked[cutoff - 1][2]
    next_key = ranked[cutoff][2]
    if border_key != next_key:
        return {"status": "resolved"}

    tied = [(reg, total) for reg, total, key in ranked if key == border_key]
    unused_stats = [stat for stat in TIEBREAKER_STATS if stat not in tiebreakers]

    if unused_stats:
        reason = "points" if not tiebreakers else "tiebreaker"
        last_label = TIEBREAKER_STATS[tiebreakers[-1]] if tiebreakers else None
        return {
            "status": "need_tiebreaker",
            "reason": reason,
            "tied": tied,
            "unused_stats": unused_stats,
            "used_stats": tiebreakers,
            "last_tiebreaker_label": last_label,
        }

    return {"status": "manual", "tied": tied, "used_stats": tiebreakers}


async def detect_tie_at_cutoff(
    db_session: AsyncSession,
    tour_id: int,
    cutoff: int,
) -> tuple[bool, list[tuple[Registration, Decimal]]]:
    analysis = await analyze_cutoff_tie(db_session, tour_id, cutoff)
    if analysis["status"] in ("resolved", "not_enough_players"):
        return False, []
    if analysis["status"] == "manual":
        return True, analysis.get("tied", [])
    return True, analysis.get("tied", [])


async def save_stage_match_results(
    db_session: AsyncSession,
    stage_id: int,
    winning_team: TeamLabel,
    mvp_team_a_id: int,
    mvp_team_b_id: int,
    player_stats: dict[int, PlayerMatchStats],
    admin_id: int | None = None,
    *,
    replace_existing: bool = False,
) -> Stage:
    existing = await db_session.scalar(
        select(func.count(StageResult.id)).where(StageResult.stage_id == stage_id)
    )
    if existing and not replace_existing:
        raise ValueError("Результаты этого матча уже внесены. Используйте редактирование.")

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        raise ValueError("Этап не найден.")

    if replace_existing and existing:
        await db_session.execute(
            StageResult.__table__.delete().where(StageResult.stage_id == stage_id)
        )

    points_win, points_mvp = await get_scoring_settings(db_session, stage.tournament_id)
    teams = await get_stage_teams_with_members(db_session, stage_id)

    for label, members in teams.items():
        for member in members:
            stats = player_stats.get(member.id)
            if not stats:
                if stage.is_final:
                    stats = empty_player_match_stats()
                else:
                    raise ValueError(f"Не внесена статистика для {member.game_nick}.")

            points = Decimal("0")
            if label == winning_team:
                points += Decimal(points_win)
            if member.id == mvp_team_a_id or member.id == mvp_team_b_id:
                points += Decimal(points_mvp)

            db_session.add(
                StageResult(
                    stage_id=stage_id,
                    registration_id=member.id,
                    team_label=label,
                    points=points,
                    placement=1 if label == winning_team else 2,
                    kills=stats.kills,
                    deaths=stats.deaths,
                    assists=stats.assists,
                    acs=stats.acs,
                    econ_rating=stats.econ_rating,
                    first_bloods=stats.first_bloods,
                    spikes_planted=stats.spikes_planted,
                    spikes_defused=stats.spikes_defused,
                    is_stage_mvp=member.id in (mvp_team_a_id, mvp_team_b_id),
                    entered_by=admin_id,
                )
            )

    stage.status = StageStatus.RESULT_ENTERED
    stage.played_at = now_moscow()
    return stage


async def finalize_stage_results(db_session: AsyncSession, stage_id: int) -> Stage:
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        raise ValueError("Этап не найден.")
    if stage.status != StageStatus.RESULT_ENTERED:
        raise ValueError("Сначала внесите или отредактируйте результаты.")
    stage.status = StageStatus.COMPLETED
    return stage


async def notify_stage_results_to_participants(
    db_session: AsyncSession,
    bot,
    stage_id: int,
) -> tuple[int, int]:
    from bot.services.stages import format_stage_teams_text

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        raise ValueError("Этап не найден.")

    results = (
        await db_session.execute(
            select(StageResult, Registration)
            .join(Registration, Registration.id == StageResult.registration_id)
            .where(StageResult.stage_id == stage_id)
        )
    ).all()
    teams_text = await format_stage_teams_text(db_session, stage)
    sent = 0
    failed = 0
    for result, registration in results:
        user_tg = (
            await db_session.execute(select(User.telegram_id).where(User.id == registration.user_id))
        ).scalar_one_or_none()
        if not user_tg:
            failed += 1
            continue
        mvp_mark = " 🏅 MVP" if result.is_stage_mvp else ""
        text = (
            f"📊 Результаты {'финала' if stage.is_final else 'этапа #{stage.stage_number}'}:\n\n"
            f"{teams_text}\n\n"
            f"Ваш результат: {result.points} б.{mvp_mark}\n"
            f"{format_match_stats_short(result.kills, result.deaths, result.assists, result.acs)}"
        )
        try:
            await bot.send_message(user_tg, text)
            sent += 1
        except Exception:
            failed += 1
    return sent, failed


RESULT_CELL_FIELDS: dict[str, tuple[str, str]] = {
    "acs": ("ACS", "decimal"),
    "kills": ("K/D/A — киллы", "int"),
    "deaths": ("K/D/A — смерти", "int"),
    "assists": ("K/D/A — ассисты", "int"),
    "econ_rating": ("Экономика", "int"),
    "first_bloods": ("Первая кровь", "int"),
    "spikes_planted": ("Spike заложено", "int"),
    "spikes_defused": ("Spike обезврежено", "int"),
}


async def get_stage_result_context(db_session: AsyncSession, stage_id: int) -> dict:
    rows = (
        await db_session.execute(
            select(StageResult, Registration)
            .join(Registration, Registration.id == StageResult.registration_id)
            .where(StageResult.stage_id == stage_id)
            .order_by(StageResult.team_label, Registration.id)
        )
    ).all()
    if not rows:
        raise ValueError("Результаты этого этапа ещё не внесены.")

    winning_team = next(
        (result.team_label for result, _ in rows if result.placement == 1),
        rows[0][0].team_label,
    )
    mvp_a_id = mvp_b_id = None
    for result, registration in rows:
        if result.is_stage_mvp and result.team_label == TeamLabel.A:
            mvp_a_id = registration.id
        if result.is_stage_mvp and result.team_label == TeamLabel.B:
            mvp_b_id = registration.id

    return {
        "winning_team": winning_team,
        "mvp_a_id": mvp_a_id,
        "mvp_b_id": mvp_b_id,
        "rows": rows,
    }


async def format_stage_results_edit_summary(
    db_session: AsyncSession,
    stage_id: int,
    *,
    admin_view: bool = True,
) -> str:
    from bot.services.stages import format_stage_teams_text

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        return "❌ Этап не найден."

    ctx = await get_stage_result_context(db_session, stage_id)
    label_fn = admin_player_label if admin_view else public_player_label
    lines = [
        await format_stage_teams_text(db_session, stage),
        "",
        f"🏆 Победитель: команда {ctx['winning_team'].value}",
    ]
    for result, registration in ctx["rows"]:
        if result.is_stage_mvp:
            lines.append(f"⭐ MVP {result.team_label.value}: {label_fn(registration)}")
    lines.append("\n📊 Статистика:")
    for result, registration in ctx["rows"]:
        lines.append(
            f"• {label_fn(registration)} ({result.team_label.value}): "
            f"{result.points} б. | {format_match_stats_short(result.kills, result.deaths, result.assists, result.acs)} | "
            f"Экон {result.econ_rating or 0} | FB {result.first_bloods or 0} | "
            f"Spike {result.spikes_planted or 0}/{result.spikes_defused or 0}"
        )
    return "\n".join(lines)


async def _recalculate_stage_result_points(
    db_session: AsyncSession,
    stage_id: int,
    winning_team: TeamLabel,
    mvp_a_id: int | None,
    mvp_b_id: int | None,
) -> None:
    points_win, points_mvp = await get_scoring_settings(
        db_session,
        (
            await db_session.execute(select(Stage.tournament_id).where(Stage.id == stage_id))
        ).scalar_one(),
    )
    results = (
        await db_session.execute(select(StageResult).where(StageResult.stage_id == stage_id))
    ).scalars().all()
    for result in results:
        points = Decimal("0")
        if result.team_label == winning_team:
            points += Decimal(points_win)
        if result.registration_id in {mvp_a_id, mvp_b_id}:
            points += Decimal(points_mvp)
        result.points = points
        result.placement = 1 if result.team_label == winning_team else 2
        result.is_stage_mvp = result.registration_id in {mvp_a_id, mvp_b_id}


async def _sync_final_awards_if_needed(db_session: AsyncSession, stage_id: int) -> None:
    from bot.services.final_stage import sync_final_awards_from_stage

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if stage and stage.is_final:
        await sync_final_awards_from_stage(db_session, stage_id)


async def update_stage_winning_team(
    db_session: AsyncSession,
    stage_id: int,
    winning_team: TeamLabel,
) -> None:
    ctx = await get_stage_result_context(db_session, stage_id)
    await _recalculate_stage_result_points(
        db_session,
        stage_id,
        winning_team,
        ctx["mvp_a_id"],
        ctx["mvp_b_id"],
    )
    await _sync_final_awards_if_needed(db_session, stage_id)


async def update_stage_mvp(
    db_session: AsyncSession,
    stage_id: int,
    team_label: TeamLabel,
    registration_id: int,
) -> None:
    ctx = await get_stage_result_context(db_session, stage_id)
    mvp_a_id = ctx["mvp_a_id"]
    mvp_b_id = ctx["mvp_b_id"]
    if team_label == TeamLabel.A:
        mvp_a_id = registration_id
    else:
        mvp_b_id = registration_id

    target = next((reg for result, reg in ctx["rows"] if reg.id == registration_id), None)
    if not target:
        raise ValueError("Игрок не найден в результатах этого этапа.")
    result_team = next(result.team_label for result, reg in ctx["rows"] if reg.id == registration_id)
    if result_team != team_label:
        raise ValueError(f"Игрок не в команде {team_label.value}.")

    await _recalculate_stage_result_points(
        db_session,
        stage_id,
        ctx["winning_team"],
        mvp_a_id,
        mvp_b_id,
    )
    await _sync_final_awards_if_needed(db_session, stage_id)


async def update_stage_result_field(
    db_session: AsyncSession,
    stage_id: int,
    registration_id: int,
    field: str,
    raw_value: str,
) -> None:
    if field not in RESULT_CELL_FIELDS:
        raise ValueError("Неизвестное поле для редактирования.")

    result = (
        await db_session.execute(
            select(StageResult).where(
                StageResult.stage_id == stage_id,
                StageResult.registration_id == registration_id,
            )
        )
    ).scalar_one_or_none()
    if not result:
        raise ValueError("Результат игрока не найден.")

    _, value_type = RESULT_CELL_FIELDS[field]
    text = raw_value.strip().replace(",", ".")
    try:
        if value_type == "decimal":
            value = Decimal(text)
        else:
            value = int(text)
            if value < 0:
                raise ValueError
    except (ValueError, ArithmeticError):
        raise ValueError(f"Некорректное значение для поля «{RESULT_CELL_FIELDS[field][0]}».")

    setattr(result, field, value)


async def format_leaderboard_text(
    db_session: AsyncSession,
    tour_id: int,
    limit: int | None = 10,
    *,
    admin_view: bool = False,
    group_stage_only: bool = True,
) -> str:
    replacements = (
        await db_session.execute(
            select(ReplacementLog).where(ReplacementLog.tournament_id == tour_id)
        )
    ).scalars().all()
    replacement_map: dict[int, ReplacementLog] = {
        log.new_registration_id: log for log in replacements
    }

    leaderboard = await get_ranked_leaderboard(
        db_session, tour_id, group_stage_only=group_stage_only
    )
    if not leaderboard:
        title = "📊 Рейтинг группового этапа:\n" if group_stage_only else "📊 Рейтинг:\n"
        return f"{title}Пока нет начисленных баллов."

    tiebreakers = await get_rating_tiebreakers(db_session, tour_id)
    header = "📊 Рейтинг группового этапа:\n" if group_stage_only else "📊 Рейтинг по баллам:\n"
    if tiebreakers:
        labels = ", ".join(TIEBREAKER_STATS[stat] for stat in tiebreakers)
        header += f"⚖️ Тай-брейки: {labels}\n"

    lines = [header]
    rows = leaderboard if limit is None else leaderboard[:limit]
    for place, registration, total in rows:
        note = ""
        if registration.id in replacement_map:
            old_reg = (
                await db_session.execute(
                    select(Registration).where(
                        Registration.id == replacement_map[registration.id].old_registration_id
                    )
                )
            ).scalar_one_or_none()
            if old_reg:
                old_label = admin_player_label(old_reg) if admin_view else public_player_label(old_reg)
                note = f" (замена вместо {old_label})"
        player_label = admin_player_label(registration) if admin_view else public_player_label(registration)
        tb_note = ""
        if tiebreakers:
            parts = []
            for stat in tiebreakers:
                tb_value = await get_player_tiebreaker_value(
                    db_session, tour_id, registration.id, stat
                )
                if stat == "kda_ratio":
                    display_value = f"{tb_value.quantize(Decimal('0.01'))}"
                elif stat == "acs":
                    display_value = format_acs(tb_value)
                else:
                    display_value = str(tb_value)
                parts.append(f"{TIEBREAKER_STATS[stat]}: {display_value}")
            tb_note = f", {', '.join(parts)}"
        lines.append(f"{place}. {player_label} — {total} б.{tb_note}{note}")
    return "\n".join(lines)


async def format_personal_stats_text(
    db_session: AsyncSession,
    tour_id: int,
    telegram_id: int,
) -> str:
    registration = (
        await db_session.execute(
            select(Registration)
            .join(User, User.id == Registration.user_id)
            .where(
                User.telegram_id == telegram_id,
                Registration.tournament_id == tour_id,
            )
            .order_by(Registration.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not registration:
        return "❌ Вы не участвуете в этом турнире."

    total = await db_session.scalar(
        select(func.coalesce(func.sum(StageResult.points), 0))
        .join(Stage, Stage.id == StageResult.stage_id)
        .where(
            StageResult.registration_id == registration.id,
            Stage.tournament_id == tour_id,
            Stage.is_final.is_(False),
        )
    ) or Decimal("0")

    ranked = await get_ranked_leaderboard(db_session, tour_id)
    place = next((item[0] for item in ranked if item[1].id == registration.id), None)

    replacement_note = ""
    replacement = (
        await db_session.execute(
            select(ReplacementLog).where(
                ReplacementLog.tournament_id == tour_id,
                ReplacementLog.new_registration_id == registration.id,
            )
        )
    ).scalar_one_or_none()
    if replacement:
        old_reg = (
            await db_session.execute(
                select(Registration).where(Registration.id == replacement.old_registration_id)
            )
        ).scalar_one_or_none()
        if old_reg:
            replacement_note = (
                f"\n♻️ Вы зашли на замену вместо {public_player_label(old_reg)}."
            )

    match_stats = (
        await db_session.execute(
            select(StageResult, Stage)
            .join(Stage, Stage.id == StageResult.stage_id)
            .where(
                StageResult.registration_id == registration.id,
                Stage.tournament_id == tour_id,
                Stage.is_final.is_(False),
            )
            .order_by(Stage.stage_number)
        )
    ).all()

    stats_lines: list[str] = []
    if match_stats:
        stats_lines.append("\n📈 Ваши матчи:")
        for result, stage in match_stats:
            stats_lines.append(
                f"\n• Этап #{stage.stage_number}: {result.points} б. | "
                f"{format_match_stats_short(result.kills, result.deaths, result.assists, result.acs)} | "
                f"Экон {result.econ_rating or 0} | "
                f"FB {result.first_bloods or 0} | "
                f"Spike {result.spikes_planted or 0}/{result.spikes_defused or 0}"
            )

    leaderboard = await format_leaderboard_text(db_session, tour_id, limit=10, admin_view=False)
    place_line = f"Место в рейтинге: {place}\n" if place else ""
    body = (
        f"📊 Ваши баллы: {total}\n"
        f"{place_line}"
        f"Riot ID: {registration.game_nick}{replacement_note}"
    )
    if stats_lines:
        body += "".join(stats_lines)
    return f"{body}\n\n{leaderboard}"


async def get_manual_pick_context(
    db_session: AsyncSession,
    tour_id: int,
    cutoff: int,
) -> dict:
    analysis = await analyze_cutoff_tie(db_session, tour_id, cutoff)
    if analysis["status"] != "manual":
        raise ValueError("manual_selection_not_required")

    tiebreakers = await get_rating_tiebreakers(db_session, tour_id)
    base = await aggregate_player_points(db_session, tour_id)
    ranked: list[tuple[Registration, Decimal, tuple]] = []
    for registration, total in base:
        sort_key = await _player_sort_key(
            db_session, tour_id, registration, total, tiebreakers
        )
        ranked.append((registration, total, sort_key))
    ranked.sort(key=lambda item: (item[2], -item[0].id), reverse=True)

    border_key = ranked[cutoff - 1][2]
    auto_finalists = [reg for reg, _, key in ranked if key > border_key]
    tied = analysis.get("tied", [])
    slots_to_pick = cutoff - len(auto_finalists)

    return {
        "auto_finalists": auto_finalists,
        "tied": tied,
        "slots_to_pick": slots_to_pick,
        "used_stats": tiebreakers,
        "border_key": border_key,
    }
