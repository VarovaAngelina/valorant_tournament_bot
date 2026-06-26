# bot/handlers/stages.py
# Обработчики группового этапа и финала:
# - дашборд этапов, формирование команд, отправка кода лобби;
# - внесение результатов, редактирование состава команд;
# - inline-навигация через edit_text (см. bot.utils.callback_ui).
from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from decimal import Decimal
from io import BytesIO
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from bot.filters.role import RoleFilter
from bot.utils.callback_ui import reply_or_edit
from bot.utils.timezone import now_moscow
from bot.services.grouping import get_group_members
from bot.services.replacements import (
    get_available_reserves,
    get_outside_roster_candidates,
    replace_group_member,
    replacement_followup_keyboard,
    send_lobby_code_to_player,
    send_participation_request,
)
from bot.services.scoreboard_parser import (
    deserialize_player_stats,
    format_scoreboard_preview,
    parse_scoreboard_image,
    serialize_player_stats,
)
from bot.services.scoring import (
    PlayerMatchStats,
    RESULT_CELL_FIELDS,
    format_leaderboard_text,
    format_stage_results_edit_summary,
    get_scoring_settings,
    get_stage_result_context,
    parse_player_match_stats,
    save_stage_match_results,
    update_scoring_settings,
    update_stage_mvp,
    update_stage_result_field,
    update_stage_winning_team,
    finalize_stage_results,
    notify_stage_results_to_participants,
    recalculate_all_tournament_points,
)
from bot.services.notifications import notify_group_stage_rating_to_participants
from bot.services.stages import (
    approve_next_cycle,
    assign_and_send_match_code,
    create_stage_with_random_teams,
    ensure_group_stage_started,
    finish_group_stage,
    format_stage_history_text,
    format_stage_teams_text,
    get_group_stage_progress,
    get_stage_outside_match_members,
    get_stage_teams_with_members,
    get_tournament_stages,
    add_player_to_stage_team,
    assign_player_to_stage_team,
    move_player_between_teams,
    prune_stage_team_roster,
    resolve_subgroup_size,
    EDITABLE_TEAM_STAGE_STATUSES,
    resend_match_code_to_group,
)
from bot.states.stages import StageAdminStates
from bot.utils.player_display import admin_player_label, public_player_label
from config import settings
from db.models import Admin, Registration, Stage, StageStatus, TeamLabel, Tournament, TournamentStatus

stages_router = Router()
stages_router.message.filter(RoleFilter("admin", "developer"))
stages_router.callback_query.filter(RoleFilter("admin", "developer"))


def _is_developer(role: str, user_id: int | None = None) -> bool:
    if role == "developer":
        return True
    return user_id is not None and user_id == settings.DEVELOPER_TG_ID


async def _reply_or_edit(
    callback: types.CallbackQuery,
    text: str,
    reply_markup: types.InlineKeyboardMarkup | None = None,
) -> None:
    await reply_or_edit(callback, text, reply_markup)


def _back_button(tour_id: int) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text="⬅️ К этапам", callback_data=f"tour_stages_{tour_id}")


def _manage_back_button(tour_id: int) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"manage_tour_{tour_id}")


def _final_back_button(tour_id: int) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text="⬅️ К финалу", callback_data=f"final_dash_{tour_id}")


def _results_back_button(stage: Stage) -> types.InlineKeyboardButton:
    if stage.is_final:
        return _final_back_button(stage.tournament_id)
    return _back_button(stage.tournament_id)


def _stage_edit_menu_keyboard(stage: Stage) -> types.InlineKeyboardMarkup:
    stage_id = stage.id
    buttons = [
        [types.InlineKeyboardButton(
            text="🏆 Победившая команда",
            callback_data=f"stage_edit_pick_{stage_id}_winner",
        )],
        [
            types.InlineKeyboardButton(
                text="⭐ MVP команды A",
                callback_data=f"stage_edit_pick_{stage_id}_mvp_a",
            ),
            types.InlineKeyboardButton(
                text="⭐ MVP команды B",
                callback_data=f"stage_edit_pick_{stage_id}_mvp_b",
            ),
        ],
        [types.InlineKeyboardButton(
            text="👤 Статистика игрока",
            callback_data=f"stage_edit_pick_{stage_id}_player",
        )],
        [types.InlineKeyboardButton(
            text="📋 Перезаписать все результаты",
            callback_data=f"stage_edit_full_{stage_id}",
        )],
    ]
    if stage.is_final:
        buttons.append([_final_back_button(stage.tournament_id)])
    else:
        buttons.append([_back_button(stage.tournament_id)])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def _open_stage_edit_menu(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    stage_id: int,
) -> None:
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await _reply_or_edit(callback, "❌ Этап не найден.", None)
        return
    if stage.status not in (StageStatus.RESULT_ENTERED, StageStatus.COMPLETED):
        await _reply_or_edit(
            callback,
            "❌ Редактирование доступно после внесения результатов.",
            None,
        )
        return

    try:
        summary = await format_stage_results_edit_summary(db_session, stage_id, admin_view=True)
    except ValueError as exc:
        await callback.message.edit_text(f"❌ {exc}")
        return

    await _reply_or_edit(
        callback,
        f"✏️ Редактирование результатов\n\n{summary}\n\nВыберите, что изменить:",
        _stage_edit_menu_keyboard(stage),
    )


def _finish_button(tour_id: int) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(
        text="🏁 Завершить групповой этап",
        callback_data=f"stage_cycle_end_{tour_id}",
    )


def _append_navigation_buttons(
    buttons: list[list[types.InlineKeyboardButton]],
    tour_id: int,
    *,
    show_finish: bool = True,
) -> None:
    if show_finish:
        buttons.append([_finish_button(tour_id)])
    buttons.append([_manage_back_button(tour_id)])


async def _get_active_stage_for_group(db_session: AsyncSession, group_id: int) -> Stage | None:
    return (
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


def _points_button(tour_id: int) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(
        text="⚙️ Изменить баллы",
        callback_data=f"stage_points_{tour_id}",
    )


async def _commit_stage_match_stats(
    actor: types.Message | types.CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    parsed_stats: dict[int, PlayerMatchStats],
    role: str,
) -> None:
    data = await state.get_data()
    stage_id = data.get("stage_id")
    winning_team = TeamLabel(data.get("winning_team", TeamLabel.A.value))
    mvp_a_id = data.get("mvp_a_id")
    mvp_b_id = data.get("mvp_b_id")

    user = actor.from_user
    message = actor if isinstance(actor, types.Message) else actor.message

    admin = (
        await db_session.execute(select(Admin).where(Admin.telegram_id == user.id))
    ).scalar_one_or_none()

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await state.clear()
        await message.answer("❌ Этап не найден.")
        return

    try:
        if stage.is_final:
            from bot.services.final_stage import save_final_match_results

            winner = await save_final_match_results(
                db_session,
                stage_id,
                winning_team,
                mvp_a_id,
                mvp_b_id,
                parsed_stats,
                admin_id=admin.id if admin else None,
            )
            await db_session.commit()
            await state.clear()
            from bot.services.notifications import notify_final_results_to_participants

            sent, failed = await notify_final_results_to_participants(
                actor.bot if isinstance(actor, types.CallbackQuery) else message.bot,
                db_session,
                stage.tournament_id,
            )
            notice = f"\n📨 Уведомлено участников: {sent}."
            if failed:
                notice += f" Не доставлено: {failed}."
            await message.answer(
                f"✅ Финал завершён. Победитель турнира: {winner.game_nick}{notice}",
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [types.InlineKeyboardButton(
                            text="✏️ Редактировать результат",
                            callback_data=f"stage_edit_results_{stage_id}",
                        )],
                        [types.InlineKeyboardButton(
                            text="🏆 Управление финалом",
                            callback_data=f"final_dash_{stage.tournament_id}",
                        )],
                        [_manage_back_button(stage.tournament_id)],
                    ]
                ),
            )
            return

        replace_existing = bool(data.get("replace_existing"))
        await save_stage_match_results(
            db_session,
            stage_id,
            winning_team,
            mvp_a_id,
            mvp_b_id,
            parsed_stats,
            admin_id=admin.id if admin else None,
            replace_existing=replace_existing,
        )
        await db_session.commit()
        await state.clear()
        tour_id = stage.tournament_id
        await message.answer(
            "✅ Результаты сохранены (статус: result_entered).\n"
            "Можно отредактировать или зафиксировать и уведомить участников.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(
                        text="✏️ Редактировать результаты",
                        callback_data=f"stage_edit_results_{stage_id}",
                    )],
                    [types.InlineKeyboardButton(
                        text="📣 Уведомить участников и зафиксировать",
                        callback_data=f"stage_finalize_{stage_id}",
                    )],
                    [_back_button(tour_id)],
                ]
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await message.answer(f"❌ {exc}")


def _manual_stats_keyboard(stage_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(
                text="✏️ Ввести вручную",
                callback_data=f"stage_stats_manual_{stage_id}",
            )
        ]]
    )


@stages_router.callback_query(
    F.data.startswith("tour_stages_") | F.data.startswith("stage_dash_")
)
async def stage_dashboard(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    if callback.id != "0":
        await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    await _open_stage_dashboard(callback, db_session, tour_id, role)


async def _build_stage_dashboard_content(
    db_session: AsyncSession,
    tour_id: int,
    role: str,
) -> tuple[str, list[list[types.InlineKeyboardButton]]]:
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        return "❌ Турнир не найден.", []

    if tour.status not in (
        TournamentStatus.GROUPS_FORMED,
        TournamentStatus.STAGE_IN_PROGRESS,
        TournamentStatus.RATING_CALCULATED,
        TournamentStatus.COMPLETED,
    ):
        return "❌ Групповой этап доступен только после формирования групп.", []

    progress = await get_group_stage_progress(db_session, tour_id)
    if tour.status in (TournamentStatus.GROUPS_FORMED, TournamentStatus.STAGE_IN_PROGRESS):
        await ensure_group_stage_started(db_session, tour_id)
        await db_session.commit()
        progress = await get_group_stage_progress(db_session, tour_id)
    history = await format_stage_history_text(db_session, tour_id)
    points_win, points_mvp = await get_scoring_settings(db_session, tour_id)
    points_line = f"\n\n⚙️ Баллы: победа +{points_win}, MVP +{points_mvp}"
    buttons: list[list[types.InlineKeyboardButton]] = []

    if (
        progress["state"] == "finished"
        or tour.status in (TournamentStatus.RATING_CALCULATED, TournamentStatus.COMPLETED)
    ):
        header = (
            "📊 Рейтинг группового этапа"
            if tour.status == TournamentStatus.COMPLETED
            else "📊 Групповой этап завершён"
        )
        text = (
            f"{header}.\n\n{history}\n\n"
            f"{await format_leaderboard_text(db_session, tour_id, limit=None, admin_view=True)}"
            f"{points_line}"
        )
        completed_stages = [
            stage for stage in await get_tournament_stages(db_session, tour_id)
            if not stage.is_final
            and stage.status in (StageStatus.RESULT_ENTERED, StageStatus.COMPLETED)
        ]
        if completed_stages:
            text += "\n\n📋 Завершённые матчи:"
            for stage in completed_stages[-8:]:
                text += f"\n• Этап #{stage.stage_number}"
        if tour.status != TournamentStatus.COMPLETED:
            buttons.append([_points_button(tour_id)])
        for stage in completed_stages[-6:]:
            buttons.append([types.InlineKeyboardButton(
                text=f"👁 Матч #{stage.stage_number}",
                callback_data=f"stage_view_{stage.id}",
            )])
        if tour.status == TournamentStatus.RATING_CALCULATED:
            buttons.append([
                types.InlineKeyboardButton(
                    text="🏅 Определить финалистов",
                    callback_data=f"finalists_select_{tour_id}",
                )
            ])
        buttons.append([_manage_back_button(tour_id)])
    elif progress["state"] == "cycle_decision":
        finished_cycles = progress.get("finished_cycles", 1)
        text = (
            f"🔁 Круг {finished_cycles} завершён.\n\n{history}\n\n"
            f"Можно начать круг {finished_cycles + 1} (каждая группа снова сыграет с новым случайным делением на команды) "
            "или завершить групповой этап и перейти к подведению итогов."
            f"{points_line}"
        )
        buttons.append([
            types.InlineKeyboardButton(
                text=f"🔁 Начать круг {finished_cycles + 1}",
                callback_data=f"stage_cycle_continue_{tour_id}",
            )
        ])
        buttons.append([_points_button(tour_id)])
        _append_navigation_buttons(buttons, tour_id)
    elif progress["state"] == "awaiting_replacement_confirm":
        group = progress["group"]
        names = ", ".join(public_player_label(item) for item in progress["unconfirmed"])
        text = (
            f"⏳ Группа {group.group_number}, круг {progress['cycle']}.\n"
            f"Нельзя начать матч, пока не подтвердят участие: {names}\n\n{history}"
            f"{points_line}"
        )
        buttons.append([
            types.InlineKeyboardButton(
                text="♻️ Заменить участника",
                callback_data=f"stage_replace_menu_{tour_id}_{group.id}",
            )
        ])
        if _is_developer(role):
            for player in progress["unconfirmed"]:
                buttons.append([
                    types.InlineKeyboardButton(
                        text=f"🧪 Подтвердить {public_player_label(player)}",
                        callback_data=f"stage_dev_confirm_{player.id}",
                    )
                ])
            buttons.append([
                types.InlineKeyboardButton(
                    text="🧪 Подтвердить всех в группе",
                    callback_data=f"stage_dev_confirm_all_{tour_id}_{group.id}",
                )
            ])
        buttons.append([_points_button(tour_id)])
        _append_navigation_buttons(buttons, tour_id)
    elif progress["state"] == "ready_to_form":
        group = progress["group"]
        text = (
            f"🎯 Следующий матч: группа {group.group_number}, круг {progress['cycle']}.\n"
            f"Нужно случайно разделить 10 участников на 2 команды по 5.\n\n{history}"
            f"{points_line}"
        )
        buttons.append([
            types.InlineKeyboardButton(
                text="🎲 Сформировать команды",
                callback_data=f"stage_form_{tour_id}_{group.id}_{progress['stage_number']}",
            )
        ])
        buttons.append([
            types.InlineKeyboardButton(
                text="♻️ Заменить участника",
                callback_data=f"stage_replace_menu_{tour_id}_{group.id}",
            )
        ])
        buttons.append([_points_button(tour_id)])
        _append_navigation_buttons(buttons, tour_id)
    elif progress["state"] == "active_stage":
        stage = progress["stage"]
        group = progress.get("group")
        teams_text = await format_stage_teams_text(db_session, stage)
        text = f"{teams_text}\n\n{history}{points_line}"

        if stage.status in (StageStatus.TEAMS_FORMED, StageStatus.CODE_SENT, StageStatus.RESULT_ENTERED):
            if stage.status in (StageStatus.TEAMS_FORMED, StageStatus.CODE_SENT):
                buttons.append([
                    types.InlineKeyboardButton(
                        text="✏️ Редактировать команды",
                        callback_data=f"stage_edit_teams_{stage.id}",
                    )
                ])
            if stage.status == StageStatus.TEAMS_FORMED:
                buttons.append([
                    types.InlineKeyboardButton(
                        text="📨 Отправить код лобби группе",
                        callback_data=f"stage_send_code_{stage.id}",
                    )
                ])
                if group:
                    buttons.append([
                        types.InlineKeyboardButton(
                            text="♻️ Заменить участника",
                            callback_data=f"stage_replace_menu_{tour_id}_{group.id}",
                        )
                    ])
            elif stage.status == StageStatus.CODE_SENT:
                buttons.append([
                    types.InlineKeyboardButton(
                        text="🏆 Внести результаты матча",
                        callback_data=f"stage_results_{stage.id}",
                    )
                ])
                buttons.append([
                    types.InlineKeyboardButton(
                        text="🔁 Повторно отправить код группе",
                        callback_data=f"stage_resend_code_group_{stage.id}",
                    )
                ])
                if group:
                    buttons.append([
                        types.InlineKeyboardButton(
                            text="♻️ Заменить участника",
                            callback_data=f"stage_replace_menu_{tour_id}_{group.id}",
                        )
                    ])
            elif stage.status == StageStatus.RESULT_ENTERED:
                buttons.append([
                    types.InlineKeyboardButton(
                        text="✏️ Редактировать результаты",
                        callback_data=f"stage_edit_results_{stage.id}",
                    )
                ])
                buttons.append([
                    types.InlineKeyboardButton(
                        text="📣 Уведомить участников и зафиксировать",
                        callback_data=f"stage_finalize_{stage.id}",
                    )
                ])
        buttons.append([_points_button(tour_id)])
        _append_navigation_buttons(buttons, tour_id)
    else:
        text = "❌ Группы ещё не сформированы."
        _append_navigation_buttons(buttons, tour_id, show_finish=False)

    if not buttons:
        _append_navigation_buttons(buttons, tour_id)

    return text, buttons


async def _open_stage_dashboard(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    tour_id: int,
    role: str,
    *,
    notice: str | None = None,
) -> None:
    text, buttons = await _build_stage_dashboard_content(db_session, tour_id, role)
    if notice:
        text = f"{notice}\n\n{text}"
    await _reply_or_edit(callback, text, types.InlineKeyboardMarkup(inline_keyboard=buttons))


@stages_router.callback_query(F.data.startswith("stage_view_"))
async def stage_view_match(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    stage_id = int(callback.data.split("_")[-1])
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await callback.message.edit_text("❌ Этап не найден.")
        return
    if stage.status not in (StageStatus.RESULT_ENTERED, StageStatus.COMPLETED):
        await callback.message.edit_text("ℹ️ Результаты этого матча ещё не внесены.")
        return

    try:
        summary = await format_stage_results_edit_summary(db_session, stage_id, admin_view=True)
    except ValueError as exc:
        await callback.message.edit_text(f"❌ {exc}")
        return

    buttons = [
        [types.InlineKeyboardButton(
            text="✏️ Редактировать",
            callback_data=f"stage_edit_results_{stage_id}",
        )],
        [_back_button(stage.tournament_id)],
    ]
    await _reply_or_edit(
        callback,
        f"👁 Просмотр матча\n\n{summary}",
        types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_form_"))
async def stage_form_teams(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    _, _, tour_id, group_id, stage_number = callback.data.split("_")
    tour_id = int(tour_id)
    group_id = int(group_id)
    stage_number = int(stage_number)

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        await callback.message.answer("❌ Турнир не найден.")
        return

    try:
        stage = await create_stage_with_random_teams(db_session, tour, group_id, stage_number)
        await db_session.commit()
        teams_text = await format_stage_teams_text(db_session, stage)
        await callback.message.answer(
            f"✅ Команды сформированы и сохранены в историю.\n\n{teams_text}",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text="📨 Отправить код", callback_data=f"stage_send_code_{stage.id}")],
                    [_back_button(tour_id)],
                ]
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@stages_router.callback_query(F.data.startswith("stage_send_code_"))
async def stage_send_code_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    stage_id = int(callback.data.split("_")[-1])
    await state.update_data(stage_id=stage_id)
    await state.set_state(StageAdminStates.waiting_for_match_code)
    await callback.message.answer("📨 Введите код приглашения в лобби:")


@stages_router.message(StageAdminStates.waiting_for_match_code)
async def stage_send_code_save(message: types.Message, state: FSMContext, db_session: AsyncSession):
    code = message.text.strip()
    if not code:
        await message.answer("❌ Код не может быть пустым.")
        return

    data = await state.get_data()
    stage_id = data.get("stage_id")
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await state.clear()
        await message.answer("❌ Этап не найден.")
        return

    try:
        sent, failed = await assign_and_send_match_code(db_session, message.bot, stage_id, code)
        await db_session.commit()
        await state.clear()
        await message.answer(
            f"✅ Код отправлен {sent} участникам группы."
            + (f" Не удалось доставить: {failed}." if failed else ""),
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text="🏆 Внести результаты",
                            callback_data=f"stage_results_{stage_id}",
                        )
                    ],
                    [
                        types.InlineKeyboardButton(
                            text="🔁 Повторно отправить код группе",
                            callback_data=f"stage_resend_code_group_{stage_id}",
                        )
                    ],
                    [_back_button(stage.tournament_id)],
                ]
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await message.answer(f"❌ {exc}")


@stages_router.callback_query(F.data.startswith("stage_results_"))
async def stage_results_pick_winner(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    await callback.answer()
    stage_id = int(callback.data.split("_")[-1])
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await callback.message.answer("❌ Этап не найден.")
        return
    await state.update_data(stage_id=stage_id)
    await callback.message.edit_text(
        "🏆 Выберите победившую команду:",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(text="Команда A", callback_data=f"stage_win_{stage_id}_A"),
                    types.InlineKeyboardButton(text="Команда B", callback_data=f"stage_win_{stage_id}_B"),
                ],
                [_back_button(stage.tournament_id)],
            ]
        ),
    )


@stages_router.callback_query(F.data.startswith("stage_win_"))
async def stage_results_pick_mvp_a(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    await callback.answer()
    _, _, stage_id, winner = callback.data.split("_")
    stage_id = int(stage_id)
    winning_team = TeamLabel.A if winner == "A" else TeamLabel.B
    await state.update_data(stage_id=stage_id, winning_team=winning_team.value)

    teams = await get_stage_teams_with_members(db_session, stage_id)
    buttons = [
        [
            types.InlineKeyboardButton(
                text=public_player_label(member),
                callback_data=f"stage_mvp_a_{stage_id}_{member.id}",
            )
        ]
        for member in teams[TeamLabel.A]
    ]
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one()
    buttons.append([_back_button(stage.tournament_id)])
    await callback.message.edit_text(
        "⭐ Выберите MVP команды A:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_mvp_a_"))
async def stage_results_pick_mvp_b(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    mvp_a_id = int(parts[4])
    await state.update_data(mvp_a_id=mvp_a_id)

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one()
    teams = await get_stage_teams_with_members(db_session, stage_id)
    buttons = [
        [
            types.InlineKeyboardButton(
                text=public_player_label(member),
                callback_data=f"stage_mvp_b_{stage_id}_{mvp_a_id}_{member.id}",
            )
        ]
        for member in teams[TeamLabel.B]
    ]
    buttons.append([_back_button(stage.tournament_id)])
    await callback.message.edit_text(
        "⭐ Выберите MVP команды B:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_mvp_b_"))
async def stage_results_start_player_stats(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    role: str,
):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    mvp_a_id = int(parts[4])
    mvp_b_id = int(parts[5])

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await callback.message.answer("❌ Этап не найден.")
        return

    data = await state.get_data()
    winning_team = data.get("winning_team", TeamLabel.A.value)
    teams = await get_stage_teams_with_members(db_session, stage_id)
    players = [
        {"id": member.id, "nick": public_player_label(member)}
        for label in (TeamLabel.A, TeamLabel.B)
        for member in teams[label]
    ]

    await state.update_data(
        stage_id=stage_id,
        winning_team=winning_team,
        mvp_a_id=mvp_a_id,
        mvp_b_id=mvp_b_id,
        stats_players=players,
    )

    if stage.is_final:
        await _commit_stage_match_stats(callback, state, db_session, {}, role)
        return

    await state.set_state(StageAdminStates.waiting_for_scoreboard_screenshot)
    await callback.message.edit_text(
        "📸 Отправьте скриншот таблицы результатов матча из Valorant.\n\n"
        "Бот сам распознает ACS, K/D/A, экономику, первую кровь и spike "
        "для всех 10 игроков.\n\n"
        "Лучше отправить скрин **как файл** (📎 → Файл), чтобы Telegram не сжал текст. "
        "Скрин должен быть чётким, без обрезки таблицы. "
        "Ники на скрине должны совпадать с игровыми никами участников турнира.",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                *_manual_stats_keyboard(stage_id).inline_keyboard,
                [_back_button(stage.tournament_id)],
            ]
        ),
        parse_mode="Markdown",
    )


@stages_router.callback_query(F.data.startswith("stage_stats_manual_"))
async def stage_stats_manual_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    stage_id = int(callback.data.split("_")[-1])
    data = await state.get_data()
    players = data.get("stats_players") or []
    if not players:
        await callback.message.answer("❌ Сессия ввода результатов истекла. Начните заново.")
        await state.clear()
        return

    await state.update_data(stats_index=0, player_stats={})
    await state.set_state(StageAdminStates.waiting_for_player_stats)
    first = players[0]
    await callback.message.answer(
        f"📊 Введите статистику для {first['nick']}:\n"
        "Формат: ACS K D A Экон FB Заложено Обезвреждено\n"
        "Пример: 334 23 19 11 73 7 1 2"
    )


async def _download_message_image(message: types.Message) -> bytes | None:
    if message.photo:
        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        file = await message.bot.get_file(message.document.file_id)
    else:
        return None

    buffer = BytesIO()
    await message.bot.download_file(file.file_path, buffer)
    return buffer.getvalue()


async def _handle_scoreboard_screenshot(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
):
    data = await state.get_data()
    stage_id = data.get("stage_id")
    if not stage_id:
        await state.clear()
        await message.answer("❌ Сессия ввода результатов истекла.")
        return

    image_bytes = await _download_message_image(message)
    if not image_bytes:
        await message.answer(
            "📸 Пришлите скрин таблицы результатов как фото или файл (PNG/JPG).",
            reply_markup=_manual_stats_keyboard(stage_id),
        )
        return

    teams = await get_stage_teams_with_members(db_session, stage_id)
    registrations = [member for label in (TeamLabel.A, TeamLabel.B) for member in teams[label]]

    await message.answer("🔍 Распознаю скрин, подождите…")

    try:
        result = await parse_scoreboard_image(image_bytes, registrations)
    except ValueError as exc:
        await message.answer(
            f"❌ {exc}",
            reply_markup=_manual_stats_keyboard(stage_id),
        )
        return

    await state.update_data(player_stats=serialize_player_stats(result.matched))
    preview = format_scoreboard_preview(result, registrations)
    await message.answer(
        preview,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="✅ Сохранить",
                        callback_data=f"stage_stats_confirm_{stage_id}",
                    ),
                    types.InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"stage_stats_cancel_{stage_id}",
                    ),
                ],
                [
                    types.InlineKeyboardButton(
                        text="✏️ Ввести вручную",
                        callback_data=f"stage_stats_manual_{stage_id}",
                    )
                ],
            ]
        ),
    )


@stages_router.message(
    StageAdminStates.waiting_for_scoreboard_screenshot,
    F.photo | (F.document & F.document.mime_type.startswith("image/")),
)
async def stage_results_from_screenshot(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
):
    await _handle_scoreboard_screenshot(message, state, db_session)


@stages_router.message(StageAdminStates.waiting_for_scoreboard_screenshot)
async def stage_results_screenshot_expected_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    stage_id = data.get("stage_id")
    await message.answer(
        "📸 Пришлите скрин таблицы результатов как фото или файл (PNG/JPG). "
        "Для лучшего распознавания лучше отправить файл без сжатия.",
        reply_markup=_manual_stats_keyboard(stage_id) if stage_id else None,
    )


@stages_router.callback_query(F.data.startswith("stage_stats_confirm_"))
async def stage_stats_confirm_save(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    role: str,
):
    await callback.answer()
    data = await state.get_data()
    raw_stats = data.get("player_stats") or {}
    if not raw_stats:
        await callback.message.answer("❌ Нет данных для сохранения.")
        return
    parsed_stats = deserialize_player_stats(raw_stats)
    await _commit_stage_match_stats(callback, state, db_session, parsed_stats, role)


@stages_router.callback_query(F.data.startswith("stage_stats_cancel_"))
async def stage_stats_cancel(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    stage_id = int(callback.data.split("_")[-1])
    await state.update_data(player_stats={})
    await callback.message.answer(
        "Отмена сохранения. Пришлите скрин ещё раз или введите данные вручную.",
        reply_markup=_manual_stats_keyboard(stage_id),
    )


@stages_router.message(StageAdminStates.waiting_for_player_stats)
async def stage_results_save_player_stats(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
    role: str,
):
    data = await state.get_data()
    players = data.get("stats_players") or []
    index = data.get("stats_index", 0)
    player_stats = dict(data.get("player_stats") or {})

    if index >= len(players):
        await state.clear()
        await message.answer("❌ Сессия ввода статистики истекла.")
        return

    current = players[index]
    try:
        stats = parse_player_match_stats(message.text)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return

    player_stats[str(current["id"])] = {
        "acs": str(stats.acs),
        "kills": stats.kills,
        "deaths": stats.deaths,
        "assists": stats.assists,
        "econ_rating": stats.econ_rating,
        "first_bloods": stats.first_bloods,
        "spikes_planted": stats.spikes_planted,
        "spikes_defused": stats.spikes_defused,
    }
    index += 1

    if index < len(players):
        await state.update_data(stats_index=index, player_stats=player_stats)
        next_player = players[index]
        await message.answer(
            f"📊 Введите статистику для {next_player['nick']}:\n"
            "Формат: ACS K D A Экон FB Заложено Обезвреждено\n"
            "Пример: 334 23 19 11 73 7 1 2"
        )
        return

    stage_id = data.get("stage_id")
    winning_team = TeamLabel(data.get("winning_team", TeamLabel.A.value))
    mvp_a_id = data.get("mvp_a_id")
    mvp_b_id = data.get("mvp_b_id")

    parsed_stats = {
        int(reg_id): PlayerMatchStats(
            acs=Decimal(values["acs"]),
            kills=values["kills"],
            deaths=values["deaths"],
            assists=values["assists"],
            econ_rating=values["econ_rating"],
            first_bloods=values["first_bloods"],
            spikes_planted=values["spikes_planted"],
            spikes_defused=values["spikes_defused"],
        )
        for reg_id, values in player_stats.items()
    }
    await _commit_stage_match_stats(message, state, db_session, parsed_stats, role)


@stages_router.callback_query(F.data.startswith("stage_cycle_continue_"))
async def stage_cycle_continue(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    next_cycle = await approve_next_cycle(db_session, tour_id)
    await db_session.commit()
    await _open_stage_dashboard(
        callback,
        db_session,
        tour_id,
        role,
        notice=f"🔁 Круг {next_cycle} одобрен. Можно формировать команды для первой группы.",
    )


@stages_router.callback_query(F.data.startswith("stage_cycle_end_confirm_"))
async def stage_cycle_end_confirm(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    await finish_group_stage(db_session, tour_id)
    await db_session.commit()
    sent, failed = await notify_group_stage_rating_to_participants(
        callback.bot, db_session, tour_id
    )
    text = await format_leaderboard_text(db_session, tour_id, limit=None, admin_view=True)
    notice = f"\n\n📨 Уведомлено участников: {sent}."
    if failed:
        notice += f" Не доставлено: {failed}."
    await callback.message.edit_text(
        f"🏁 Групповой этап завершён.\n\n{text}{notice}",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[_manage_back_button(tour_id)]]
        ),
    )


@stages_router.callback_query(F.data.startswith("stage_cycle_end_cancel_"))
async def stage_cycle_end_cancel(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    await callback.message.edit_text("Отмена завершения группового этапа.")
    fake = types.CallbackQuery(
        id="0",
        from_user=callback.from_user,
        chat_instance="0",
        message=callback.message,
        data=f"stage_dash_{tour_id}",
    )
    await stage_dashboard(fake, db_session, role)


@stages_router.callback_query(F.data.startswith("stage_cycle_end_"))
async def stage_cycle_end_prompt(callback: types.CallbackQuery):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    await callback.message.answer(
        "⚠️ Завершить групповой этап?\n\n"
        "Начисленные баллы сохранятся, новые матчи проводиться не будут.",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text="✅ Да, завершить",
                        callback_data=f"stage_cycle_end_confirm_{tour_id}",
                    ),
                    types.InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"stage_cycle_end_cancel_{tour_id}",
                    ),
                ]
            ]
        ),
    )


@stages_router.callback_query(F.data.startswith("stage_replace_menu_"))
async def stage_replace_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    group_id = int(parts[4])
    members = await get_group_members(db_session, group_id)
    buttons = [
        [
            types.InlineKeyboardButton(
                text=f"Заменить {public_player_label(member)}",
                callback_data=f"stage_replace_pick_{tour_id}_{group_id}_{member.id}",
            )
        ]
        for member in members
    ]
    buttons.append([_back_button(tour_id)])
    await callback.message.answer(
        "♻️ Выберите участника группы для замены:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_replace_pick_"))
async def stage_replace_choose_source(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    group_id = int(parts[4])
    old_reg_id = int(parts[5])

    buttons = [
        [types.InlineKeyboardButton(
            text="🔄 Резерв",
            callback_data=f"stage_repl_res_{tour_id}_{group_id}_{old_reg_id}",
        )],
        [types.InlineKeyboardButton(
            text="⚪ Вне матча",
            callback_data=f"stage_repl_out_{tour_id}_{group_id}_{old_reg_id}",
        )],
        [types.InlineKeyboardButton(
            text="➕ Создать нового участника",
            callback_data=f"stage_repl_new_{tour_id}_{group_id}_{old_reg_id}",
        )],
        [_back_button(tour_id)],
    ]
    await callback.message.edit_text(
        "Выберите источник замены:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_repl_res_"))
async def stage_replace_reserve_list(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    group_id = int(parts[4])
    old_reg_id = int(parts[5])

    reserves = await get_available_reserves(db_session, tour_id)
    if not reserves:
        await callback.message.edit_text(
            "❌ Резерв пуст.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=f"stage_replace_pick_{tour_id}_{group_id}_{old_reg_id}",
                    )
                ]]
            ),
        )
        return

    buttons = [
        [types.InlineKeyboardButton(
            text=f"🔄 {admin_player_label(reserve)}",
            callback_data=f"stage_replace_do_{tour_id}_{group_id}_{old_reg_id}_{reserve.id}",
        )]
        for reserve in reserves
    ]
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"stage_replace_pick_{tour_id}_{group_id}_{old_reg_id}",
    )])
    await callback.message.edit_text(
        "Выберите игрока из резерва:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_repl_out_"))
async def stage_replace_outside_list(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    group_id = int(parts[4])
    old_reg_id = int(parts[5])

    outside = await get_outside_roster_candidates(db_session, tour_id)
    if not outside:
        await callback.message.edit_text(
            "❌ Список «Вне матча» пуст.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=f"stage_replace_pick_{tour_id}_{group_id}_{old_reg_id}",
                    )
                ]]
            ),
        )
        return

    buttons = [
        [types.InlineKeyboardButton(
            text=f"⚪ {admin_player_label(candidate)}",
            callback_data=f"stage_replace_do_{tour_id}_{group_id}_{old_reg_id}_{candidate.id}",
        )]
        for candidate in outside
    ]
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"stage_replace_pick_{tour_id}_{group_id}_{old_reg_id}",
    )])
    await callback.message.edit_text(
        "Выберите игрока из списка «Вне матча»:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_repl_new_"))
async def stage_replace_new_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    group_id = int(parts[4])
    old_reg_id = int(parts[5])

    await state.update_data(
        manual_tour_id=tour_id,
        manual_flow="stage_replace",
        manual_replace_group_id=group_id,
        manual_replace_old_reg_id=old_reg_id,
    )
    from bot.states.admin import AdminTournamentStates

    await state.set_state(AdminTournamentStates.waiting_for_manual_tg_id)
    await callback.message.edit_text(
        "➕ Создание нового участника для замены.\n\n"
        "Введите Telegram ID или @username:"
    )


@stages_router.callback_query(F.data.startswith("stage_replace_do_"))
async def stage_replace_execute(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[3])
    group_id = int(parts[4])
    old_reg_id = int(parts[5])
    new_reg_id = int(parts[6])

    admin = (
        await db_session.execute(select(Admin).where(Admin.telegram_id == callback.from_user.id))
    ).scalar_one_or_none()

    try:
        new_reg = await replace_group_member(
            db_session,
            callback.bot,
            tour_id,
            group_id,
            old_reg_id,
            new_reg_id,
            admin_id=admin.id if admin else None,
        )
        await db_session.commit()
        active_stage = await _get_active_stage_for_group(db_session, group_id)
        await callback.message.edit_text(
            f"✅ {public_player_label(new_reg)} поставлен на замену.\n"
            "При необходимости отправьте подтверждение и код лобби.",
            reply_markup=replacement_followup_keyboard(
                tour_id,
                group_id,
                new_reg.id,
                stage_id=active_stage.id if active_stage else None,
                show_dev_confirm=_is_developer(role, callback.from_user.id),
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@stages_router.callback_query(F.data.startswith("stage_dev_confirm_all_"))
async def stage_dev_confirm_all(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    from datetime import datetime

    if not _is_developer(role, callback.from_user.id):
        await callback.answer("Только для разработчика", show_alert=True)
        return

    await callback.answer()
    parts = callback.data.split("_")
    tour_id = int(parts[4])
    group_id = int(parts[5])

    members = await get_group_members(db_session, group_id)
    for member in members:
        if not member.participation_confirmed:
            member.participation_confirmed = True
            member.participation_confirmed_at = now_moscow()

    await db_session.commit()
    await _open_stage_dashboard(callback, db_session, tour_id, role)


@stages_router.callback_query(F.data.regexp(r"^stage_dev_confirm_\d+$"))
async def stage_dev_confirm_one(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    from datetime import datetime

    if not _is_developer(role, callback.from_user.id):
        await callback.answer("Только для разработчика", show_alert=True)
        return

    await callback.answer()
    registration_id = int(callback.data.split("_")[-1])
    reg = (
        await db_session.execute(select(Registration).where(Registration.id == registration_id))
    ).scalar_one_or_none()
    if not reg:
        await callback.message.edit_text("❌ Участник не найден.")
        return

    if not reg.participation_confirmed:
        reg.participation_confirmed = True
        reg.participation_confirmed_at = now_moscow()
        await db_session.commit()

    await _open_stage_dashboard(callback, db_session, reg.tournament_id, role)


@stages_router.callback_query(F.data.startswith("stage_resend_confirm_"))
async def stage_resend_confirm(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    role: str,
):
    await callback.answer()
    registration_id = int(callback.data.split("_")[-1])
    sent = await send_participation_request(db_session, callback.bot, registration_id)
    notice = (
        "✅ Запрос на подтверждение участия отправлен."
        if sent
        else "❌ Не удалось отправить запрос на подтверждение."
    )
    reg = (
        await db_session.execute(select(Registration).where(Registration.id == registration_id))
    ).scalar_one_or_none()
    if reg:
        await _open_stage_dashboard(
            callback, db_session, reg.tournament_id, role, notice=notice,
        )
    else:
        await _reply_or_edit(callback, notice, None)


@stages_router.callback_query(F.data.startswith("stage_resend_code_player_"))
async def stage_resend_code_player(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    role: str,
):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[4])
    registration_id = int(parts[5])
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    try:
        await send_lobby_code_to_player(db_session, callback.bot, stage_id, registration_id)
        notice = "✅ Код лобби отправлен игроку."
    except ValueError as exc:
        notice = f"❌ {exc}"
    if stage:
        await _open_stage_dashboard(
            callback, db_session, stage.tournament_id, role, notice=notice,
        )
    else:
        await _reply_or_edit(callback, notice, None)


@stages_router.callback_query(F.data.startswith("stage_resend_code_group_"))
async def stage_resend_code_group(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    role: str,
):
    await callback.answer()
    stage_id = int(callback.data.split("_")[-1])
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await _reply_or_edit(callback, "❌ Этап не найден.", None)
        return
    try:
        sent, failed = await resend_match_code_to_group(db_session, callback.bot, stage_id)
        await db_session.commit()
        notice = (
            f"✅ Код повторно отправлен {sent} участникам группы."
            + (f" Не удалось доставить: {failed}." if failed else "")
        )
    except ValueError as exc:
        await db_session.rollback()
        notice = f"❌ {exc}"
    await _open_stage_dashboard(
        callback, db_session, stage.tournament_id, role, notice=notice,
    )


@stages_router.callback_query(F.data.startswith("stage_points_"))
async def stage_points_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    await state.update_data(tour_id=tour_id)
    await state.set_state(StageAdminStates.waiting_for_points_win)
    await callback.message.answer("⚙️ Введите количество баллов за победу в матче:")


@stages_router.message(StageAdminStates.waiting_for_points_win)
async def stage_points_save_win(message: types.Message, state: FSMContext):
    try:
        points_win = int(message.text.strip())
        if points_win < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое неотрицательное число.")
        return
    await state.update_data(points_win=points_win)
    await state.set_state(StageAdminStates.waiting_for_points_mvp)
    await message.answer("⚙️ Введите количество баллов за MVP:")


@stages_router.message(StageAdminStates.waiting_for_points_mvp)
async def stage_points_save_mvp(message: types.Message, state: FSMContext, db_session: AsyncSession):
    try:
        points_mvp = int(message.text.strip())
        if points_mvp < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое неотрицательное число.")
        return

    data = await state.get_data()
    tour_id = data.get("tour_id")
    points_win = data.get("points_win")
    if tour_id is None or points_win is None:
        await state.clear()
        await message.answer("❌ Сессия настройки баллов истекла.")
        return

    await update_scoring_settings(db_session, tour_id, points_win, points_mvp)
    updated_stages = await recalculate_all_tournament_points(db_session, tour_id)
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ Баллы обновлены: победа +{points_win}, MVP +{points_mvp}.\n"
        f"Пересчитано матчей: {updated_stages}.",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[_back_button(tour_id)]]
        ),
    )


@stages_router.callback_query(F.data.startswith("stage_edit_results_"))
async def stage_edit_results(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    stage_id = int(callback.data.split("_")[-1])
    await _open_stage_edit_menu(callback, db_session, stage_id)


@stages_router.callback_query(F.data.startswith("stage_edit_menu_"))
async def stage_edit_menu_back(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    stage_id = int(callback.data.split("_")[-1])
    await _open_stage_edit_menu(callback, db_session, stage_id)


@stages_router.callback_query(F.data.startswith("stage_edit_full_"))
async def stage_edit_full(callback: types.CallbackQuery, state: FSMContext, db_session: AsyncSession):
    await callback.answer()
    stage_id = int(callback.data.split("_")[-1])
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await callback.message.answer("❌ Этап не найден.")
        return

    await state.update_data(stage_id=stage_id, replace_existing=True)
    await callback.message.answer(
        "📋 Полная перезапись результатов. Выберите победившую команду:",
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(text="Команда A", callback_data=f"stage_win_{stage_id}_A"),
                    types.InlineKeyboardButton(text="Команда B", callback_data=f"stage_win_{stage_id}_B"),
                ],
                [_results_back_button(stage)],
            ]
        ),
    )


@stages_router.callback_query(F.data.startswith("stage_edit_pick_"))
async def stage_edit_pick_field(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    payload = callback.data.removeprefix("stage_edit_pick_")
    stage_id_str, field = payload.split("_", 1)
    stage_id = int(stage_id_str)

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await callback.message.answer("❌ Этап не найден.")
        return

    try:
        ctx = await get_stage_result_context(db_session, stage_id)
    except ValueError as exc:
        await callback.message.edit_text(f"❌ {exc}")
        return

    menu_back = types.InlineKeyboardButton(
        text="⬅️ К выбору поля",
        callback_data=f"stage_edit_menu_{stage_id}",
    )

    if field == "winner":
        await callback.message.edit_text(
            "🏆 Выберите победившую команду:",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text="Команда A",
                            callback_data=f"stage_edit_set_{stage_id}_winner_A",
                        ),
                        types.InlineKeyboardButton(
                            text="Команда B",
                            callback_data=f"stage_edit_set_{stage_id}_winner_B",
                        ),
                    ],
                    [menu_back],
                ]
            ),
        )
        return

    if field in ("mvp_a", "mvp_b"):
        team = TeamLabel.A if field == "mvp_a" else TeamLabel.B
        suffix = "mvpa" if field == "mvp_a" else "mvpb"
        members = [reg for result, reg in ctx["rows"] if result.team_label == team]
        buttons = [
            [types.InlineKeyboardButton(
                text=public_player_label(member),
                callback_data=f"stage_edit_set_{stage_id}_{suffix}_{member.id}",
            )]
            for member in members
        ]
        buttons.append([menu_back])
        await callback.message.edit_text(
            f"⭐ Выберите MVP команды {team.value}:",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        return

    if field == "player":
        buttons = [
            [types.InlineKeyboardButton(
                text=f"{public_player_label(reg)} ({result.team_label.value})",
                callback_data=f"stage_edit_player_{stage_id}_{reg.id}",
            )]
            for result, reg in ctx["rows"]
        ]
        buttons.append([menu_back])
        await callback.message.edit_text(
            "👤 Выберите игрока для редактирования статистики:",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        return

    await callback.message.answer("❌ Неизвестный тип редактирования.")


@stages_router.callback_query(F.data.startswith("stage_edit_set_"))
async def stage_edit_set_value(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    payload = callback.data.removeprefix("stage_edit_set_")
    stage_id_str, rest = payload.split("_", 1)
    stage_id = int(stage_id_str)

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await callback.message.answer("❌ Этап не найден.")
        return

    try:
        if rest.startswith("winner_"):
            team = TeamLabel(rest.split("_", 1)[1])
            await update_stage_winning_team(db_session, stage_id, team)
        elif rest.startswith("mvpa_"):
            await update_stage_mvp(db_session, stage_id, TeamLabel.A, int(rest.split("_", 1)[1]))
        elif rest.startswith("mvpb_"):
            await update_stage_mvp(db_session, stage_id, TeamLabel.B, int(rest.split("_", 1)[1]))
        else:
            await callback.message.answer("❌ Неизвестный тип редактирования.")
            return
        await db_session.commit()
        await callback.message.answer("✅ Изменение сохранено.")
        await _open_stage_edit_menu(callback, db_session, stage_id)
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@stages_router.callback_query(F.data.startswith("stage_edit_player_"))
async def stage_edit_player_fields(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    reg_id = int(parts[4])

    registration = (
        await db_session.execute(select(Registration).where(Registration.id == reg_id))
    ).scalar_one_or_none()
    if not registration:
        await callback.message.answer("❌ Игрок не найден.")
        return

    buttons = [
        [types.InlineKeyboardButton(
            text=label,
            callback_data=f"stage_edit_cell_{stage_id}_{reg_id}_{field}",
        )]
        for field, (label, _) in RESULT_CELL_FIELDS.items()
    ]
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ К выбору поля",
        callback_data=f"stage_edit_menu_{stage_id}",
    )])
    await callback.message.edit_text(
        f"✏️ {public_player_label(registration)}\nВыберите ячейку для редактирования:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_edit_cell_"))
async def stage_edit_cell_start(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    reg_id = int(parts[4])
    field = parts[5]

    if field not in RESULT_CELL_FIELDS:
        await callback.message.answer("❌ Неизвестное поле.")
        return

    result = (
        await db_session.execute(
            select(StageResult).where(
                StageResult.stage_id == stage_id,
                StageResult.registration_id == reg_id,
            )
        )
    ).scalar_one_or_none()
    registration = (
        await db_session.execute(select(Registration).where(Registration.id == reg_id))
    ).scalar_one_or_none()
    if not result or not registration:
        await callback.message.answer("❌ Результат не найден.")
        return

    label, _ = RESULT_CELL_FIELDS[field]
    current = getattr(result, field)
    await state.update_data(
        edit_stage_id=stage_id,
        edit_reg_id=reg_id,
        edit_field=field,
    )
    await state.set_state(StageAdminStates.waiting_for_edit_cell_value)
    await callback.message.answer(
        f"✏️ {public_player_label(registration)}\n"
        f"Поле: {label}\n"
        f"Текущее значение: {current}\n\n"
        "Введите новое значение:"
    )


@stages_router.message(StageAdminStates.waiting_for_edit_cell_value)
async def stage_edit_cell_save(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
):
    data = await state.get_data()
    stage_id = data.get("edit_stage_id")
    reg_id = data.get("edit_reg_id")
    field = data.get("edit_field")
    if not stage_id or not reg_id or not field:
        await state.clear()
        await message.answer("❌ Сессия редактирования истекла.")
        return

    try:
        await update_stage_result_field(db_session, stage_id, reg_id, field, message.text)
        await db_session.commit()
        await state.clear()
        stage = (
            await db_session.execute(select(Stage).where(Stage.id == stage_id))
        ).scalar_one()
        await message.answer(
            "✅ Значение обновлено.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(
                        text="✏️ Продолжить редактирование",
                        callback_data=f"stage_edit_menu_{stage_id}",
                    )],
                    [_results_back_button(stage)],
                ]
            ),
        )
    except ValueError as exc:
        await db_session.rollback()
        await message.answer(f"❌ {exc}")


@stages_router.callback_query(F.data.startswith("stage_finalize_"))
async def stage_finalize(callback: types.CallbackQuery, db_session: AsyncSession, role: str):
    await callback.answer()
    stage_id = int(callback.data.split("_")[-1])
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await callback.message.answer("❌ Этап не найден.")
        return

    try:
        await finalize_stage_results(db_session, stage_id)
        sent, failed = await notify_stage_results_to_participants(
            db_session, callback.bot, stage_id
        )
        await db_session.commit()
        await callback.message.answer(
            f"✅ Этап зафиксирован (completed). Уведомлено участников: {sent}."
            + (f" Не доставлено: {failed}." if failed else "")
        )
        fake = types.CallbackQuery(
            id="0",
            from_user=callback.from_user,
            chat_instance="0",
            message=callback.message,
            data=f"stage_dash_{stage.tournament_id}",
        )
        await stage_dashboard(fake, db_session, role)
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@stages_router.callback_query(F.data.startswith("stage_edit_teams_"))
async def stage_edit_teams_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    stage_id = int(callback.data.split("_")[-1])
    text, buttons = await _build_stage_edit_teams_root(db_session, stage_id)
    if not buttons:
        await callback.message.edit_text(text)
        return
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


def _stage_team_back_button(stage_id: int, team_label: TeamLabel) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"stage_team_view_{stage_id}_{team_label.value}",
    )


async def _prepare_editable_stage(
    db_session: AsyncSession,
    stage_id: int,
) -> tuple[Stage | None, dict[TeamLabel, list[Registration]], str, int, list[Registration]]:
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        return None, {TeamLabel.A: [], TeamLabel.B: []}, "❌ Этап не найден.", 5, []

    if stage.status not in EDITABLE_TEAM_STAGE_STATUSES:
        return (
            stage,
            {TeamLabel.A: [], TeamLabel.B: []},
            "❌ Редактирование команд доступно только в активном раунде до внесения результатов.",
            5,
            [],
        )

    removed = await prune_stage_team_roster(db_session, stage_id)
    if removed:
        await db_session.commit()

    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == stage.tournament_id))
    ).scalar_one_or_none()
    subgroup_size = resolve_subgroup_size(tour)
    teams = await get_stage_teams_with_members(db_session, stage_id)
    outside = await get_stage_outside_match_members(db_session, stage_id)
    teams_text = await format_stage_teams_text(db_session, stage)
    return stage, teams, teams_text, subgroup_size, outside


async def _build_stage_edit_teams_root(
    db_session: AsyncSession,
    stage_id: int,
) -> tuple[str, list[list[types.InlineKeyboardButton]]]:
    stage, teams, teams_text, subgroup_size, _outside = await _prepare_editable_stage(
        db_session, stage_id
    )
    if not stage:
        return teams_text, []

    if stage.status not in EDITABLE_TEAM_STAGE_STATUSES:
        return teams_text, [[_back_button(stage.tournament_id)]]

    buttons = [
        [types.InlineKeyboardButton(
            text=f"Команда A ({len(teams[TeamLabel.A])}/{subgroup_size})",
            callback_data=f"stage_team_view_{stage_id}_{TeamLabel.A.value}",
        )],
        [types.InlineKeyboardButton(
            text=f"Команда B ({len(teams[TeamLabel.B])}/{subgroup_size})",
            callback_data=f"stage_team_view_{stage_id}_{TeamLabel.B.value}",
        )],
        [_back_button(stage.tournament_id)],
    ]
    return (
        f"{teams_text}\n\n✏️ Выберите команду для редактирования:",
        buttons,
    )


async def _build_stage_team_view(
    db_session: AsyncSession,
    stage_id: int,
    team_label: TeamLabel,
) -> tuple[str, list[list[types.InlineKeyboardButton]]]:
    stage, teams, teams_text, subgroup_size, _outside = await _prepare_editable_stage(
        db_session, stage_id
    )
    if not stage:
        return teams_text, []

    if stage.status not in EDITABLE_TEAM_STAGE_STATUSES:
        return teams_text, [[_back_button(stage.tournament_id)]]

    other = TeamLabel.B if team_label == TeamLabel.A else TeamLabel.A
    member_count = len(teams[team_label])
    buttons: list[list[types.InlineKeyboardButton]] = []
    for member in teams[team_label]:
        buttons.append([types.InlineKeyboardButton(
            text=f"👤 {admin_player_label(member)}",
            callback_data=f"stage_team_pick_{stage_id}_{team_label.value}_{member.id}",
        )])
    if member_count < subgroup_size:
        buttons.append([types.InlineKeyboardButton(
            text="➕ Добавить участника",
            callback_data=f"stage_team_addmenu_{stage_id}_{team_label.value}",
        )])
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"stage_edit_teams_{stage_id}",
    )])

    hint = (
        f"Команда {team_label.value}: {member_count} из {subgroup_size}. "
        "Выберите участника или добавьте нового."
        if member_count < subgroup_size
        else f"Команда {team_label.value}: {member_count} из {subgroup_size}. Выберите участника:"
    )
    return f"{teams_text}\n\n{hint}", buttons


async def _render_stage_team_view(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    stage_id: int,
    team_label: TeamLabel,
    *,
    extra_text: str = "",
) -> None:
    text, buttons = await _build_stage_team_view(db_session, stage_id, team_label)
    if extra_text:
        text = f"{text}\n\n{extra_text}"
    await callback.message.edit_text(
        text,
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_team_view_"))
async def stage_team_view(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = TeamLabel(parts[4])
    await _render_stage_team_view(callback, db_session, stage_id, team_label)


@stages_router.callback_query(F.data.startswith("stage_team_pick_"))
async def stage_team_member_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = TeamLabel(parts[4])
    reg_id = int(parts[5])

    reg = (
        await db_session.execute(select(Registration).where(Registration.id == reg_id))
    ).scalar_one_or_none()
    if not reg:
        await callback.message.edit_text("❌ Участник не найден.")
        return

    other = TeamLabel.B if team_label == TeamLabel.A else TeamLabel.A
    buttons = [
        [types.InlineKeyboardButton(
            text="♻️ Заменить",
            callback_data=f"stage_team_replsrc_{stage_id}_{team_label.value}_{reg_id}",
        )],
        [types.InlineKeyboardButton(
            text=f"➡️ Перенести в команду {other.value}",
            callback_data=f"stage_team_xfer_{stage_id}_{reg_id}_{other.value}",
        )],
        [_stage_team_back_button(stage_id, team_label)],
    ]
    await callback.message.edit_text(
        f"👤 {admin_player_label(reg)}\n"
        f"Команда: {team_label.value}\n\nВыберите действие:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_team_xfer_"))
async def stage_team_xfer_player(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    reg_id = int(parts[4])
    target_label = TeamLabel(parts[5])
    source_label = TeamLabel.B if target_label == TeamLabel.A else TeamLabel.A

    try:
        await move_player_between_teams(db_session, stage_id, reg_id, target_label)
        await db_session.commit()
        await _render_stage_team_view(
            callback,
            db_session,
            stage_id,
            source_label,
            extra_text=f"✅ Участник перенесён в команду {target_label.value}.",
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@stages_router.callback_query(F.data.startswith("stage_team_replsrc_"))
async def stage_team_replace_source(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = TeamLabel(parts[4])
    old_reg_id = int(parts[5])

    buttons = [
        [types.InlineKeyboardButton(
            text="🔄 Резерв",
            callback_data=f"stage_team_replres_{stage_id}_{team_label.value}_{old_reg_id}",
        )],
        [types.InlineKeyboardButton(
            text="⚪ Вне матча",
            callback_data=f"stage_team_replout_{stage_id}_{team_label.value}_{old_reg_id}",
        )],
        [types.InlineKeyboardButton(
            text="➕ Создать нового участника",
            callback_data=f"stage_team_replnew_{stage_id}_{team_label.value}_{old_reg_id}",
        )],
        [_stage_team_back_button(stage_id, team_label)],
    ]
    await callback.message.edit_text(
        "♻️ Выберите источник замены:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_team_replres_"))
async def stage_team_replace_reserve_list(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = TeamLabel(parts[4])
    old_reg_id = int(parts[5])
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await callback.message.edit_text("❌ Этап не найден.")
        return

    reserves = await get_available_reserves(db_session, stage.tournament_id)
    if not reserves:
        await callback.message.edit_text(
            "❌ Резерв пуст.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=f"stage_team_replsrc_{stage_id}_{team_label.value}_{old_reg_id}",
                    )
                ]]
            ),
        )
        return

    buttons = [
        [types.InlineKeyboardButton(
            text=f"🔄 {admin_player_label(reserve)}",
            callback_data=f"stage_team_repldo_{stage_id}_{team_label.value}_{old_reg_id}_{reserve.id}",
        )]
        for reserve in reserves
    ]
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"stage_team_replsrc_{stage_id}_{team_label.value}_{old_reg_id}",
    )])
    await callback.message.edit_text(
        "Выберите игрока из резерва:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_team_replout_"))
async def stage_team_replace_outside_list(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = TeamLabel(parts[4])
    old_reg_id = int(parts[5])
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await callback.message.edit_text("❌ Этап не найден.")
        return

    outside = await get_outside_roster_candidates(db_session, stage.tournament_id)
    if not outside:
        await callback.message.edit_text(
            "❌ Список «Вне матча» пуст.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[
                    types.InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=f"stage_team_replsrc_{stage_id}_{team_label.value}_{old_reg_id}",
                    )
                ]]
            ),
        )
        return

    buttons = [
        [types.InlineKeyboardButton(
            text=f"⚪ {admin_player_label(candidate)}",
            callback_data=f"stage_team_repldo_{stage_id}_{team_label.value}_{old_reg_id}_{candidate.id}",
        )]
        for candidate in outside
    ]
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"stage_team_replsrc_{stage_id}_{team_label.value}_{old_reg_id}",
    )])
    await callback.message.edit_text(
        "Выберите игрока из списка «Вне матча»:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_team_replnew_"))
async def stage_team_replace_new_start(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = parts[4]
    old_reg_id = int(parts[5])
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage or not stage.group_id:
        await callback.message.edit_text("❌ Этап не найден.")
        return

    await state.update_data(
        manual_tour_id=stage.tournament_id,
        manual_flow="stage_replace",
        manual_replace_group_id=stage.group_id,
        manual_replace_old_reg_id=old_reg_id,
        manual_stage_team_id=stage_id,
        manual_stage_team_label=team_label,
    )
    from bot.states.admin import AdminTournamentStates

    await state.set_state(AdminTournamentStates.waiting_for_manual_tg_id)
    await callback.message.edit_text(
        "➕ Создание нового участника для замены.\n\n"
        "Введите Telegram ID или @username:"
    )


@stages_router.callback_query(F.data.startswith("stage_team_repldo_"))
async def stage_team_replace_execute(
    callback: types.CallbackQuery,
    db_session: AsyncSession,
    role: str,
):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = TeamLabel(parts[4])
    old_reg_id = int(parts[5])
    new_reg_id = int(parts[6])

    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage or not stage.group_id:
        await callback.message.edit_text("❌ Этап не найден.")
        return

    admin = (
        await db_session.execute(select(Admin).where(Admin.telegram_id == callback.from_user.id))
    ).scalar_one_or_none()

    try:
        new_reg = await replace_group_member(
            db_session,
            callback.bot,
            stage.tournament_id,
            stage.group_id,
            old_reg_id,
            new_reg_id,
            admin_id=admin.id if admin else None,
        )
        await db_session.commit()
        code_sent = False
        if stage.status == StageStatus.CODE_SENT and stage.match_code:
            try:
                await send_lobby_code_to_player(db_session, callback.bot, stage_id, new_reg.id)
                code_sent = True
            except Exception:
                pass
        extra = f"✅ {public_player_label(new_reg)} поставлен на замену."
        if code_sent:
            extra += " Код лобби отправлен."
        await _render_stage_team_view(
            callback, db_session, stage_id, team_label, extra_text=extra,
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")


@stages_router.callback_query(F.data.startswith("stage_team_addmenu_"))
async def stage_team_add_menu(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = TeamLabel(parts[4])

    buttons = [
        [types.InlineKeyboardButton(
            text="🔄 Резерв",
            callback_data=f"stage_team_addres_{stage_id}_{team_label.value}",
        )],
        [types.InlineKeyboardButton(
            text="⚪ Вне матча",
            callback_data=f"stage_team_addout_{stage_id}_{team_label.value}",
        )],
        [types.InlineKeyboardButton(
            text="➕ Создать нового участника",
            callback_data=f"stage_team_addnew_{stage_id}_{team_label.value}",
        )],
        [_stage_team_back_button(stage_id, team_label)],
    ]
    await callback.message.edit_text(
        f"➕ Добавление участника в команду {team_label.value}:\n\nВыберите источник:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_team_addres_"))
async def stage_team_add_reserve_list(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = TeamLabel(parts[4])
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await callback.message.edit_text("❌ Этап не найден.")
        return

    reserves = await get_available_reserves(db_session, stage.tournament_id)
    if not reserves:
        await callback.message.edit_text(
            "❌ Резерв пуст.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[_stage_team_back_button(stage_id, team_label)]]
            ),
        )
        return

    buttons = [
        [types.InlineKeyboardButton(
            text=f"🔄 {admin_player_label(reserve)}",
            callback_data=f"stage_team_adddo_{stage_id}_{team_label.value}_{reserve.id}",
        )]
        for reserve in reserves
    ]
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"stage_team_addmenu_{stage_id}_{team_label.value}",
    )])
    await callback.message.edit_text(
        "Выберите игрока из резерва:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_team_addout_"))
async def stage_team_add_outside_list(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = TeamLabel(parts[4])
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage:
        await callback.message.edit_text("❌ Этап не найден.")
        return

    outside = await get_outside_roster_candidates(db_session, stage.tournament_id)
    if not outside:
        await callback.message.edit_text(
            "❌ Список «Вне матча» пуст.",
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[[_stage_team_back_button(stage_id, team_label)]]
            ),
        )
        return

    buttons = [
        [types.InlineKeyboardButton(
            text=f"⚪ {admin_player_label(candidate)}",
            callback_data=f"stage_team_adddo_{stage_id}_{team_label.value}_{candidate.id}",
        )]
        for candidate in outside
    ]
    buttons.append([types.InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"stage_team_addmenu_{stage_id}_{team_label.value}",
    )])
    await callback.message.edit_text(
        "Выберите игрока из списка «Вне матча»:",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@stages_router.callback_query(F.data.startswith("stage_team_addnew_"))
async def stage_team_add_new_start(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = parts[4]
    stage = (
        await db_session.execute(select(Stage).where(Stage.id == stage_id))
    ).scalar_one_or_none()
    if not stage or not stage.group_id:
        await callback.message.edit_text("❌ Этап не найден.")
        return

    await state.update_data(
        manual_tour_id=stage.tournament_id,
        manual_flow="stage_team_add",
        manual_stage_id=stage_id,
        manual_stage_team_label=team_label,
        manual_add_group_id=stage.group_id,
    )
    from bot.states.admin import AdminTournamentStates

    await state.set_state(AdminTournamentStates.waiting_for_manual_tg_id)
    await callback.message.edit_text(
        f"➕ Создание нового участника для команды {team_label}.\n\n"
        "Введите Telegram ID или @username:"
    )


@stages_router.callback_query(F.data.startswith("stage_team_adddo_"))
async def stage_team_add_player(callback: types.CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    parts = callback.data.split("_")
    stage_id = int(parts[3])
    team_label = TeamLabel(parts[4])
    reg_id = int(parts[5])

    admin = (
        await db_session.execute(select(Admin).where(Admin.telegram_id == callback.from_user.id))
    ).scalar_one_or_none()

    try:
        await assign_player_to_stage_team(
            db_session,
            callback.bot,
            stage_id,
            reg_id,
            team_label,
            admin_id=admin.id if admin else None,
        )
        await db_session.commit()

        stage = (
            await db_session.execute(select(Stage).where(Stage.id == stage_id))
        ).scalar_one()
        code_sent = False
        if stage.status == StageStatus.CODE_SENT and stage.match_code:
            try:
                await send_lobby_code_to_player(db_session, callback.bot, stage_id, reg_id)
                code_sent = True
            except Exception:
                pass

        extra = "✅ Участник добавлен в команду."
        if code_sent:
            extra += " Код лобби отправлен."
        await _render_stage_team_view(
            callback, db_session, stage_id, team_label, extra_text=extra,
        )
    except ValueError as exc:
        await db_session.rollback()
        await callback.message.edit_text(f"❌ {exc}")
