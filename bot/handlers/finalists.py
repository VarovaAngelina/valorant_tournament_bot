from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from bot.filters.role import RoleFilter
from bot.services.finalists import (
    add_finalist_manual,
    assign_finalists_from_rating,
    assign_manual_finalists,
    format_finalists_text,
    get_finalists,
    remove_finalist_manual,
    replace_finalist_with_next,
    send_finalist_confirmations,
)
from bot.services.scoring import (
    TIEBREAKER_STATS,
    analyze_cutoff_tie,
    append_rating_tiebreaker,
    format_leaderboard_text,
    get_manual_pick_context,
    get_rating_tiebreakers,
)
from bot.services.tournament_meta import get_tournament_meta, save_tournament_meta
from db.models import Finalist, StageStatus, Tournament, TournamentStatus

finalists_router = Router()
finalists_router.callback_query.filter(RoleFilter("admin", "developer"))
finalists_router.message.filter(RoleFilter("admin", "developer"))


def _manage_back_button(tour_id: int) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"manage_tour_{tour_id}")


def _format_tied_names(tied: list) -> str:
    return ", ".join(player.game_nick for player, _ in tied[:8]) + (
        "..." if len(tied) > 8 else ""
    )


async def _tiebreaker_picker_markup(
    db_session: AsyncSession,
    tour_id: int,
    analysis: dict,
) -> types.InlineKeyboardMarkup:
    used = set(await get_rating_tiebreakers(db_session, tour_id))
    unused = [stat for stat in TIEBREAKER_STATS if stat not in used]
    buttons = [
        [types.InlineKeyboardButton(
            text=TIEBREAKER_STATS[stat],
            callback_data=f"finalists_tiebreak_{tour_id}_{stat}",
        )]
        for stat in unused
    ]
    buttons.append([_manage_back_button(tour_id)])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def _tiebreaker_prompt_text(tour: Tournament, analysis: dict) -> str:
    tied = analysis.get("tied", [])
    names = _format_tied_names(tied)
    reason = analysis.get("reason", "points")
    used_stats = analysis.get("used_stats", [])

    if reason == "points" or not used_stats:
        return (
            f"⚖️ На границе финала ({tour.final_size}-е место) одинаковые баллы.\n"
            f"Участники с равным счётом: {names}\n\n"
            "Выберите дополнительную характеристику для определения места:"
        )

    last_label = analysis.get("last_tiebreaker_label") or TIEBREAKER_STATS.get(used_stats[-1], "")
    used_labels = ", ".join(TIEBREAKER_STATS[stat] for stat in used_stats)
    return (
        f"⚖️ После тай-брейков ({used_labels}) счёт на границе финала "
        f"({tour.final_size}-е место) всё ещё одинаковый.\n"
        f"Показатель «{last_label}» тоже совпал у: {names}\n\n"
        "Выберите следующий дополнительный параметр:"
    )


async def _manual_picker_markup(
    db_session: AsyncSession,
    tour_id: int,
    *,
    selected_ids: list[int] | None = None,
) -> tuple[str, types.InlineKeyboardMarkup]:
    from bot.services.scoring import aggregate_player_points

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        raise ValueError("Турнир не найден.")

    ctx = await get_manual_pick_context(db_session, tour_id, tour.final_size)
    meta = await get_tournament_meta(db_session, tour_id)
    picks = selected_ids if selected_ids is not None else meta.get("manual_finalist_picks", [])

    points_map = dict(await aggregate_player_points(db_session, tour_id))
    auto_lines = [
        f"• {reg.game_nick} — {points_map.get(reg.id, 0)} б."
        for reg in ctx["auto_finalists"]
    ]

    tied = ctx["tied"]
    slots = ctx["slots_to_pick"]
    used_labels = ", ".join(TIEBREAKER_STATS[s] for s in ctx["used_stats"]) or "нет"

    text = (
        f"⚖️ Все доступные тай-брейки ({used_labels}) не позволяют однозначно "
        f"определить топ-{tour.final_size}.\n\n"
        f"✅ Автоматически в финале ({len(ctx['auto_finalists'])}):\n"
        + ("\n".join(auto_lines) if auto_lines else "— нет")
        + f"\n\n👇 Выберите {slots} из {len(tied)} игроков с полностью одинаковым счётом:\n"
        f"Отмечено: {len(picks)}/{slots}"
    )

    buttons: list[list[types.InlineKeyboardButton]] = []
    for reg, total in tied:
        mark = "✅" if reg.id in picks else "⬜"
        buttons.append([types.InlineKeyboardButton(
            text=f"{mark} {reg.game_nick} ({total} б.)",
            callback_data=f"finalists_manual_toggle_{tour_id}_{reg.id}",
        )])

    if len(picks) == slots:
        buttons.append([types.InlineKeyboardButton(
            text="✅ Подтвердить состав финалистов",
            callback_data=f"finalists_manual_confirm_{tour_id}",
        )])
    buttons.append([_manage_back_button(tour_id)])
    return text, types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def _resolve_finalists_selection(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    tour_id: int,
) -> None:
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        await callback.message.edit_text("❌ Турнир не найден.")
        return

    analysis = await analyze_cutoff_tie(db_session, tour_id, tour.final_size)

    if analysis["status"] == "not_enough_players":
        await callback.message.edit_text(
            f"❌ Недостаточно игроков с баллами для финала "
            f"({tour.final_size} нужно).\n"
            "Сначала завершите групповой этап и внесите результаты всех раундов.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[_manage_back_button(tour_id)]]
            ),
        )
        return

    if analysis["status"] == "need_tiebreaker":
        text = _tiebreaker_prompt_text(tour, analysis)
        markup = await _tiebreaker_picker_markup(db_session, tour_id, analysis)
        await callback.message.edit_text(text, reply_markup=markup)
        return

    if analysis["status"] == "manual":
        meta = await get_tournament_meta(db_session, tour_id)
        if "manual_finalist_picks" not in meta:
            meta["manual_finalist_picks"] = []
            await save_tournament_meta(db_session, tour_id, meta)
        text, markup = await _manual_picker_markup(db_session, tour_id)
        await callback.message.edit_text(text, reply_markup=markup)
        return

    try:
        count, _ = await assign_finalists_from_rating(db_session, tour_id)
        meta = await get_tournament_meta(db_session, tour_id)
        meta.pop("manual_finalist_picks", None)
        await save_tournament_meta(db_session, tour_id, meta)
        await db_session.commit()
        text = await format_finalists_text(db_session, tour_id, admin_view=True)
        await callback.message.edit_text(
            f"✅ Определено финалистов: {count}.\n\n{text}",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[_manage_back_button(tour_id)]]
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@finalists_router.callback_query(F.data.startswith("finalists_select_"))
async def finalists_select(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    await _resolve_finalists_selection(callback, db_session, tour_id)


@finalists_router.callback_query(F.data.startswith("finalists_tiebreak_"))
async def finalists_set_tiebreaker(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[2])
    stat = "_".join(parts[3:])

    try:
        await append_rating_tiebreaker(db_session, tour_id, stat)
        await db_session.commit()
    except ValueError as exc:
        await db_session.rollback()
        if str(exc) == "tiebreaker_already_used":
            await callback.message.edit_text("❌ Этот параметр тай-брейка уже использован.")
        else:
            await callback.message.edit_text(f"❌ {exc}")
        return

    await _resolve_finalists_selection(callback, db_session, tour_id)


@finalists_router.callback_query(F.data.startswith("finalists_manual_toggle_"))
async def finalists_manual_toggle(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    reg_id = int(parts[4])

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        await callback.message.edit_text("❌ Турнир не найден.")
        return

    try:
        ctx = await get_manual_pick_context(db_session, tour_id, tour.final_size)
    except ValueError as exc:
        await callback.message.edit_text(f"❌ {exc}")
        return

    tied_ids = {reg.id for reg, _ in ctx["tied"]}
    if reg_id not in tied_ids:
        await callback.answer("Игрок не в группе с равным счётом", show_alert=True)
        return

    meta = await get_tournament_meta(db_session, tour_id)
    picks: list[int] = list(meta.get("manual_finalist_picks", []))

    if reg_id in picks:
        picks.remove(reg_id)
    elif len(picks) >= ctx["slots_to_pick"]:
        await callback.answer(
            f"Можно выбрать не более {ctx['slots_to_pick']} игрок(ов)",
            show_alert=True,
        )
        return
    else:
        picks.append(reg_id)

    meta["manual_finalist_picks"] = picks
    await save_tournament_meta(db_session, tour_id, meta)
    await db_session.commit()

    text, markup = await _manual_picker_markup(db_session, tour_id, selected_ids=picks)
    await callback.message.edit_text(text, reply_markup=markup)


@finalists_router.callback_query(F.data.startswith("finalists_manual_confirm_"))
async def finalists_manual_confirm(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        await callback.message.edit_text("❌ Турнир не найден.")
        return

    meta = await get_tournament_meta(db_session, tour_id)
    picks: list[int] = meta.get("manual_finalist_picks", [])

    try:
        ctx = await get_manual_pick_context(db_session, tour_id, tour.final_size)
        if len(picks) != ctx["slots_to_pick"]:
            raise ValueError(
                f"Выберите ровно {ctx['slots_to_pick']} игрок(ов) из группы с равным счётом."
            )
        auto_ids = [reg.id for reg in ctx["auto_finalists"]]
        count = await assign_manual_finalists(db_session, tour_id, auto_ids + picks)
        meta.pop("manual_finalist_picks", None)
        await save_tournament_meta(db_session, tour_id, meta)
        await db_session.commit()
        text = await format_finalists_text(db_session, tour_id, admin_view=True)
        await callback.message.edit_text(
            f"✅ Состав финалистов определён администратором ({count} чел.).\n\n{text}",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[_manage_back_button(tour_id)]]
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@finalists_router.callback_query(F.data.startswith("finalists_confirm_send_"))
async def finalists_send_confirmations(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    try:
        sent, failed = await send_finalist_confirmations(db_session, callback.bot, tour_id)
        await callback.message.edit_text(
            f"✅ Запросы на подтверждение отправлены: {sent}."
            + (f" Не удалось: {failed}." if failed else ""),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[_manage_back_button(tour_id)]]
            ),
        )
    except ValueError as exc:
        await callback.message.edit_text(f"❌ {exc}")


@finalists_router.callback_query(F.data.startswith("finalists_view_"))
async def finalists_view(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    *,
    notice: str | None = None,
):
    if callback.id != "0":
        await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    finalists_text = await format_finalists_text(db_session, tour_id, admin_view=True)
    rating_text = await format_leaderboard_text(
        db_session, tour_id, limit=None, admin_view=True, group_stage_only=True
    )
    buttons = [
        [types.InlineKeyboardButton(
            text="➕ Добавить финалиста",
            callback_data=f"finalists_add_menu_{tour_id}",
        )],
        [types.InlineKeyboardButton(
            text="➖ Удалить финалиста",
            callback_data=f"finalists_remove_menu_{tour_id}",
        )],
        [_manage_back_button(tour_id)],
    ]
    text = f"{finalists_text}\n\n{rating_text}"
    if notice:
        text = f"{notice}\n\n{text}"
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@finalists_router.callback_query(F.data.startswith("finalists_add_menu_"))
async def finalists_add_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.scoring import get_ranked_leaderboard

    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        return

    finalist_ids = {
        item.registration_id
        for item in (
            await db_session.execute(select(Finalist).where(Finalist.tournament_id == tour_id))
        ).scalars().all()
    }
    ranked = await get_ranked_leaderboard(db_session, tour_id)
    candidates = [
        (registration, total)
        for _, registration, total in ranked
        if registration.id not in finalist_ids
    ][:15]

    if not candidates:
        await callback.message.answer("❌ Нет доступных игроков для добавления.")
        return

    buttons = [
        [types.InlineKeyboardButton(
            text=f"{registration.game_nick} ({total} б.)",
            callback_data=f"finalists_add_do_{tour_id}_{registration.id}",
        )]
        for registration, total in candidates
    ]
    buttons.append([_manage_back_button(tour_id)])
    await callback.message.edit_text(
        "➕ Выберите игрока для ручного добавления в финалисты:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@finalists_router.callback_query(F.data.startswith("finalists_add_do_"))
async def finalists_add_do(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    reg_id = int(parts[4])
    try:
        await add_finalist_manual(db_session, tour_id, reg_id)
        await db_session.commit()
        await finalists_view(
            callback,
            db_session,
            notice="✅ Финалист добавлен (source: manual_admin).",
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@finalists_router.callback_query(F.data.startswith("finalists_remove_menu_"))
async def finalists_remove_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    finalists = await get_finalists(db_session, tour_id)
    if not finalists:
        await callback.message.answer("❌ Список финалистов пуст.")
        return

    buttons = [
        [types.InlineKeyboardButton(
            text=f"➖ {registration.game_nick}",
            callback_data=f"finalists_remove_do_{finalist.id}",
        )]
        for finalist, registration in finalists
    ]
    buttons.append([_manage_back_button(tour_id)])
    await callback.message.edit_text(
        "➖ Выберите финалиста для удаления:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@finalists_router.callback_query(F.data.startswith("finalists_remove_do_"))
async def finalists_remove_do(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    finalist_id = int(callback.data.split("_")[-1])
    finalist = (
        await db_session.execute(select(Finalist).where(Finalist.id == finalist_id))
    ).scalar_one_or_none()
    if not finalist:
        await callback.message.edit_text("❌ Финалист не найден.")
        return

    try:
        await remove_finalist_manual(db_session, finalist.tournament_id, finalist_id)
        await db_session.commit()
        await finalists_view(
            callback,
            db_session,
            notice="✅ Финалист удалён из списка.",
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@finalists_router.callback_query(F.data.startswith("finalists_replace_menu_"))
async def finalists_replace_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    finalists = await get_finalists(db_session, tour_id)
    unconfirmed = [
        (finalist, registration)
        for finalist, registration in finalists
        if not finalist.participation_confirmed
    ]
    if not unconfirmed:
        await callback.message.edit_text("❌ Все финалисты уже подтвердили участие.")
        return

    buttons = [
        [types.InlineKeyboardButton(
            text=f"Заменить {registration.game_nick}",
            callback_data=f"finalists_replace_do_{finalist.id}",
        )]
        for finalist, registration in unconfirmed
    ]
    buttons.append([_manage_back_button(tour_id)])
    await callback.message.edit_text(
        "♻️ Выберите финалиста для замены следующим игроком из рейтинга:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@finalists_router.callback_query(F.data.startswith("finalists_replace_do_"))
async def finalists_replace_do(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    finalist_id = int(callback.data.split("_")[-1])
    finalist = (
        await db_session.execute(select(Finalist).where(Finalist.id == finalist_id))
    ).scalar_one_or_none()
    if not finalist:
        await callback.message.edit_text("❌ Финалист не найден.")
        return

    try:
        replacement = await replace_finalist_with_next(
            db_session, finalist.tournament_id, finalist_id
        )
        await db_session.commit()
        await callback.message.edit_text(
            f"✅ {replacement.game_nick} поставлен вместо не подтвердившего финалиста.\n"
            "Отправьте ему запрос на подтверждение.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="📨 Отправить подтверждение",
                        callback_data=f"finalists_resend_one_{finalist.id}",
                    )
                ], [_manage_back_button(finalist.tournament_id)]]
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@finalists_router.callback_query(F.data.startswith("finalists_resend_one_"))
async def finalists_resend_one(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.finalists import confirm_finalist_keyboard
    from db.models import Registration, User

    await callback.answer()
    finalist_id = int(callback.data.split("_")[-1])
    row = (
        await db_session.execute(
            select(Finalist, Registration)
            .join(Registration, Registration.id == Finalist.registration_id)
            .where(Finalist.id == finalist_id)
        )
    ).first()
    if not row:
        await callback.message.edit_text("❌ Финалист не найден.")
        return

    finalist, registration = row
    user = (
        await db_session.execute(select(User).where(User.id == registration.user_id))
    ).scalar_one_or_none()
    if not user:
        await callback.message.edit_text("❌ Telegram-профиль игрока не найден.")
        return

    try:
        await callback.bot.send_message(
            user.telegram_id,
            "🏅 Вас включили в список финалистов.\n"
            "Подтвердите участие в финале кнопкой ниже.",
            reply_markup=confirm_finalist_keyboard(finalist.id),
        )
        await callback.message.edit_text("✅ Запрос на подтверждение отправлен.")
    except Exception:
        await callback.message.edit_text("❌ Не удалось отправить сообщение игроку.")


@finalists_router.callback_query(F.data.startswith("final_dash_"))
async def final_dashboard(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.final_stage import (
        all_finalists_confirmed,
        get_final_stage,
        get_finalists_confirmation_stats,
    )
    from bot.services.stages import format_stage_teams_text
    from bot.services.tournament_meta import get_tournament_meta

    if callback.id != "0":
        await callback.answer()
    tour_id = int(callback.data.split("_")[-1])

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        await callback.message.edit_text("❌ Турнир не найден.")
        return

    if not await all_finalists_confirmed(db_session, tour_id):
        confirmed, total = await get_finalists_confirmation_stats(db_session, tour_id)
        await callback.message.edit_text(
            f"⏳ Нельзя начать финал: подтвердили {confirmed}/{total} финалистов."
        )
        return

    stage = await get_final_stage(db_session, tour_id)
    meta = await get_tournament_meta(db_session, tour_id)
    buttons: list[list[types.InlineKeyboardButton]] = []

    final_results_ready = bool(
        meta.get("final_completed")
        or (
            stage
            and stage.status in (StageStatus.RESULT_ENTERED, StageStatus.COMPLETED)
        )
    )

    if final_results_ready:
        from bot.services.final_stage import format_winner_text

        text = await format_winner_text(db_session, tour_id, admin_view=True)
        if stage and stage.status == StageStatus.RESULT_ENTERED:
            buttons.append([
                types.InlineKeyboardButton(
                    text="✏️ Редактировать результат финала",
                    callback_data=f"stage_edit_results_{stage.id}",
                )
            ])
        buttons.append([
            types.InlineKeyboardButton(
                text="✅ Завершить турнир",
                callback_data=f"tour_complete_{tour_id}",
            )
        ])
        buttons.append([_manage_back_button(tour_id)])
    elif not stage:
        text = "🏆 Все финалисты подтвердили участие.\nСформируйте команды финала."
        buttons.append([
            types.InlineKeyboardButton(
                text="🎲 Сформировать команды финала",
                callback_data=f"final_form_{tour_id}",
            )
        ])
        buttons.append([_manage_back_button(tour_id)])
    elif stage.status == StageStatus.TEAMS_FORMED:
        teams_text = await format_stage_teams_text(db_session, stage)
        text = f"🏆 Финал\n\n{teams_text}"
        buttons.append([
            types.InlineKeyboardButton(
                text="📨 Отправить код финалистам",
                callback_data=f"stage_send_code_{stage.id}",
            )
        ])
        buttons.append([_manage_back_button(tour_id)])
    elif stage.status == StageStatus.CODE_SENT:
        teams_text = await format_stage_teams_text(db_session, stage)
        text = f"🏆 Финал — код отправлен\n\n{teams_text}"
        buttons.append([
            types.InlineKeyboardButton(
                text="🏆 Внести результат финала (MVP)",
                callback_data=f"stage_results_{stage.id}",
            )
        ])
        buttons.append([
            types.InlineKeyboardButton(
                text="🔁 Повторно отправить код",
                callback_data=f"stage_resend_code_group_{stage.id}",
            )
        ])
        buttons.append([_manage_back_button(tour_id)])
    else:
        text = "🏆 Финал"
        buttons.append([_manage_back_button(tour_id)])

    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@finalists_router.callback_query(F.data.startswith("final_form_"))
async def final_form_teams(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.final_stage import create_final_stage_teams

    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        await callback.message.edit_text("❌ Турнир не найден.")
        return

    try:
        stage = await create_final_stage_teams(db_session, tour)
        await db_session.commit()
        fake = types.CallbackQuery(
            id="0",
            from_user=callback.from_user,
            chat_instance="0",
            message=callback.message,
            data=f"final_dash_{tour_id}",
        )
        await final_dashboard(fake, db_session)
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@finalists_router.callback_query(F.data.startswith("final_winner_"))
async def final_winner_view(callback: types.CallbackQuery, db_session: AsyncSession):
    from bot.services.final_stage import format_winner_text

    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    text = await format_winner_text(db_session, tour_id, admin_view=True)
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(
                    text="✅ Завершить турнир",
                    callback_data=f"tour_complete_{tour_id}",
                )],
                [_manage_back_button(tour_id)],
            ]
        ),
    )


@finalists_router.callback_query(F.data.startswith("tour_complete_"))
async def tour_complete(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    from bot.services.final_stage import complete_tournament

    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    try:
        await complete_tournament(db_session, tour_id)
        await db_session.commit()
        await callback.message.edit_text("✅ Турнир завершён.")
        from bot.handlers.admin import manage_single_tournament
        await manage_single_tournament(callback, db_session, role)
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")
