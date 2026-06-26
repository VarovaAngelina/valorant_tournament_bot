# bot/handlers/registration.py
from bot.utils.timezone import now_moscow

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from bot.keyboards.ranks import get_ranks_keyboard, get_tiers_keyboard
from bot.services.rules import get_global_rules_url, rules_registration_keyboard
from bot.services.subscription import (
    build_subscription_required_message,
    check_user_subscription,
)
from bot.services.user_menu import build_user_inline_menu
from bot.states.registration import RegistrationStates
from db.models import (
    User,
    Registration,
    Tournament,
    TournamentStatus,
    RegistrationStatus,
    SubscriptionStatus,
)

registration_router = Router()


async def _start_registration_form(
    message: types.Message,
    state: FSMContext,
    *,
    edit_in_place: bool = False,
) -> None:
    prompt = (
        "Начинаем регистрацию! 🚀\n\n"
        "Шаг 1: Введите ваш Riot ID в формате Имя#TAG (например, Player#EUW):"
    )
    if edit_in_place:
        await message.edit_text(prompt)
    else:
        await message.answer(prompt)
    await state.set_state(RegistrationStates.waiting_for_riot_id)


async def _begin_registration(
    message: types.Message,
    state: FSMContext,
    db_session: AsyncSession,
    bot,
    user: types.User,
    *,
    edit_in_place: bool = False,
) -> None:
    telegram_id = user.id
    query = select(Tournament).where(Tournament.status == TournamentStatus.REGISTRATION_OPEN)
    active_tour = (await db_session.execute(query)).scalar_one_or_none()

    if not active_tour:
        text = "❌ На данный момент нет турниров с открытой регистрацией."
        if edit_in_place:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return

    db_user = (
        await db_session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if db_user:
        existing_reg = (
            await db_session.execute(
                select(Registration).where(
                    Registration.tournament_id == active_tour.id,
                    Registration.user_id == db_user.id,
                    Registration.status.in_(
                        (
                            RegistrationStatus.REGISTERED,
                            RegistrationStatus.SELECTED_MAIN,
                            RegistrationStatus.SELECTED_RESERVE,
                        )
                    ),
                )
            )
        ).scalar_one_or_none()
        if existing_reg:
            text = (
                "ℹ️ Вы уже подали заявку на этот турнир.\n"
                "Используйте «✏️ Профиль» или «❌ Отозвать заявку»."
            )
            if edit_in_place:
                await message.edit_text(text)
            else:
                await message.answer(text)
            return

    subscribed, error_code = await check_user_subscription(
        bot,
        active_tour.channel_id,
        telegram_id,
        channel_username=active_tour.channel_username,
    )
    if not subscribed:
        text, markup = await build_subscription_required_message(
            bot,
            active_tour,
            error_code=error_code,
        )
        if edit_in_place:
            await message.edit_text(text, reply_markup=markup)
        else:
            await message.answer(text, reply_markup=markup)
        return

    rules_url = await get_global_rules_url(db_session)
    if not rules_url:
        text = "❌ Регламент ещё не опубликован администратором."
        if edit_in_place:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return

    text = (
        f"📜 Перед регистрацией в турнире «{active_tour.title}» "
        "ознакомьтесь с регламентом и примите условия:"
    )
    markup = rules_registration_keyboard(active_tour.id, rules_url)
    await state.update_data(reg_tour_id=active_tour.id)
    if edit_in_place:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@registration_router.callback_query(F.data == "user_menu_register")
async def start_registration_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
):
    await callback.answer()
    await _begin_registration(
        callback.message,
        state,
        db_session,
        callback.bot,
        callback.from_user,
        edit_in_place=True,
    )


@registration_router.message(RegistrationStates.waiting_for_riot_id)
async def process_riot_id(message: types.Message, state: FSMContext):
    riot_id = message.text.strip()
    if "#" not in riot_id or len(riot_id) < 5:
        await message.answer("❌ Неверный формат Riot ID. Пожалуйста, введите в формате Имя#TAG:")
        return

    await state.update_data(riot_id=riot_id)
    await message.answer("Шаг 2: Выберите ваш текущий ранг в Valorant:", reply_markup=get_ranks_keyboard())
    await state.set_state(RegistrationStates.waiting_for_rank)


@registration_router.callback_query(RegistrationStates.waiting_for_rank, F.data.startswith("rank_"))
async def process_rank(callback: types.CallbackQuery, state: FSMContext, role: str, db_session: AsyncSession):
    selected_rank = callback.data.split("_")[1].capitalize()
    await callback.answer()

    if selected_rank == "Radiant":
        user_data = await state.get_data()
        riot_id = user_data.get("riot_id")

        await callback.message.edit_text(f"✅ Ранг выбран: {selected_rank}")

        await send_final_registration_message(
            callback.message, callback.from_user, riot_id, selected_rank, role, db_session, state, callback.bot
        )
    else:
        await state.update_data(main_rank=selected_rank)
        await callback.message.edit_text(
            f"Вы выбрали ранг: {selected_rank}\nШаг 2.1. Выберите ступень:",
            reply_markup=get_tiers_keyboard(selected_rank),
        )
        await state.set_state(RegistrationStates.waiting_for_rank_tier)


@registration_router.callback_query(RegistrationStates.waiting_for_rank_tier, F.data.startswith("tier_"))
async def process_tier(callback: types.CallbackQuery, state: FSMContext, role: str, db_session: AsyncSession):
    _, rank_name, tier_num = callback.data.split("_")
    full_rank = f"{rank_name} {tier_num}"

    user_data = await state.get_data()
    riot_id = user_data.get("riot_id")

    await callback.answer()
    await callback.message.edit_text(f"✅ Ранг выбран: {full_rank}")

    await send_final_registration_message(
        callback.message, callback.from_user, riot_id, full_rank, role, db_session, state, callback.bot
    )


async def send_final_registration_message(
    message: types.Message,
    tg_user: types.User,
    riot_id: str,
    rank: str,
    role: str,
    db_session: AsyncSession,
    state: FSMContext,
    bot,
):
    user_display = f"@{tg_user.username}" if tg_user.username else tg_user.full_name
    if not tg_user.username:
        await message.answer(
            "⚠️ У вас не указан @username в Telegram. Администратору будет сложнее связаться с вами."
        )

    state_data = await state.get_data()
    tour_id = state_data.get("reg_tour_id")

    try:
        query_user = select(User).where(User.telegram_id == tg_user.id)
        db_user = (await db_session.execute(query_user)).scalar_one_or_none()

        if not db_user:
            db_user = User(
                telegram_id=tg_user.id,
                telegram_username=tg_user.username,
            )
            db_session.add(db_user)
            await db_session.flush()
        else:
            db_user.telegram_username = tg_user.username

        if tour_id:
            active_tournament = (
                await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
            ).scalar_one_or_none()
        else:
            active_tournament = (
                await db_session.execute(
                    select(Tournament).where(Tournament.status == TournamentStatus.REGISTRATION_OPEN)
                )
            ).scalar_one_or_none()

        if not active_tournament:
            menu = await build_user_inline_menu(db_session, tg_user.id, role, state)
            await message.edit_text(
                "❌ К сожалению, сейчас нет активных турниров с открытой регистрацией.\n"
                "Ваш профиль сохранен, но заявка не создана.",
                reply_markup=menu,
            )
            await db_session.commit()
            await state.clear()
            return

        subscribed, error_code = await check_user_subscription(
            bot,
            active_tournament.channel_id,
            tg_user.id,
            channel_username=active_tournament.channel_username,
        )
        if not subscribed:
            text, markup = await build_subscription_required_message(
                bot,
                active_tournament,
                error_code=error_code,
            )
            await db_session.rollback()
            await message.edit_text(text, reply_markup=markup)
            await state.clear()
            return

        query_reg = select(Registration).where(
            Registration.tournament_id == active_tournament.id,
            Registration.user_id == db_user.id,
        )
        db_reg = (await db_session.execute(query_reg)).scalar_one_or_none()

        if db_reg:
            db_reg.game_nick = riot_id
            db_reg.game_rank = rank
            db_reg.contact_telegram = user_display
            db_reg.status = RegistrationStatus.REGISTERED
            db_reg.rules_accepted = True
            db_reg.rules_accepted_at = now_moscow()
            db_reg.subscription_status = SubscriptionStatus.SUBSCRIBED
        else:
            db_reg = Registration(
                tournament_id=active_tournament.id,
                user_id=db_user.id,
                game_nick=riot_id,
                game_rank=rank,
                contact_telegram=user_display,
                status=RegistrationStatus.REGISTERED,
                subscription_status=SubscriptionStatus.SUBSCRIBED,
                rules_accepted=True,
                rules_accepted_at=now_moscow(),
            )
            db_session.add(db_reg)

        await db_session.commit()

    except Exception:
        await db_session.rollback()
        await message.edit_text("❌ Произошла ошибка при записи данных. Попробуйте позже.")
        raise

    await state.clear()
    menu = await build_user_inline_menu(db_session, tg_user.id, role, state)
    await message.answer(
        "🎉 Регистрация успешно завершена! 🎉\n\n"
        f"📋 Твои данные сохранены в системе:\n"
        f"• Турнир: {active_tournament.title}\n"
        f"• Аккаунт: {user_display}\n"
        f"• Riot ID: {riot_id}\n"
        f"• Полный ранг: {rank}\n\n"
        "Вы внесены в список кандидатов турнира. Ожидайте проведения отбора администратором!",
        reply_markup=menu,
    )
