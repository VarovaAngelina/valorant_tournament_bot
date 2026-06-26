from aiogram import F, Router
from aiogram.types import ChatMemberUpdated
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from bot.services.rules import get_global_rules_url, rules_registration_keyboard
from bot.services.subscription import (
    build_subscription_required_message,
    check_user_subscription,
    get_active_registration_for_user,
    sync_registration_subscription,
)
from bot.states.registration import RegistrationStates
from db.models import SubscriptionEventSource, Tournament
from aiogram.fsm.context import FSMContext

subscription_router = Router()


@subscription_router.callback_query(F.data.startswith("sub_check_"))
async def subscription_recheck(
    callback,
    db_session: AsyncSession,
    state: FSMContext,
):
    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        await callback.message.edit_text("❌ Турнир не найден.")
        return

    subscribed, error_code = await check_user_subscription(
        callback.bot,
        tour.channel_id,
        callback.from_user.id,
        channel_username=tour.channel_username,
    )
    if subscribed:
        rules_url = await get_global_rules_url(db_session)
        if not rules_url:
            await callback.message.edit_text(
                "❌ Регламент ещё не опубликован администратором."
            )
            return

        await callback.message.edit_text(
            "📜 Ознакомьтесь с регламентом и примите условия для регистрации:",
            reply_markup=rules_registration_keyboard(tour_id, rules_url),
        )
        await state.update_data(reg_tour_id=tour_id)
    else:
        text, markup = await build_subscription_required_message(
            callback.bot,
            tour,
            error_code=error_code,
        )
        await callback.message.edit_text(text, reply_markup=markup)


@subscription_router.callback_query(F.data.startswith("rules_accept_"))
async def rules_accept(
    callback,
    state: FSMContext,
    db_session: AsyncSession,
):
    from bot.handlers.registration import _start_registration_form

    await callback.answer()
    tour_id = int(callback.data.split("_")[-1])
    tour = (
        await db_session.execute(select(Tournament).where(Tournament.id == tour_id))
    ).scalar_one_or_none()
    if not tour:
        await callback.message.edit_text("❌ Турнир не найден.")
        return

    subscribed, error_code = await check_user_subscription(
        callback.bot,
        tour.channel_id,
        callback.from_user.id,
        channel_username=tour.channel_username,
    )
    if not subscribed:
        text, markup = await build_subscription_required_message(
            callback.bot,
            tour,
            error_code=error_code,
        )
        await callback.message.edit_text(text, reply_markup=markup)
        return

    await state.update_data(reg_tour_id=tour_id, rules_accepted_pending=True)
    await _start_registration_form(callback.message, state, edit_in_place=True)


@subscription_router.my_chat_member()
async def bot_chat_member_update(event: ChatMemberUpdated):
    pass


@subscription_router.chat_member()
async def user_chat_member_update(
    event: ChatMemberUpdated,
    db_session: AsyncSession,
):
    if not event.new_chat_member or not event.old_chat_member:
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return

    row = await get_active_registration_for_user(db_session, user.id)
    if not row:
        return

    tour, registration = row
    await sync_registration_subscription(
        db_session,
        event.bot,
        registration,
        tour,
        source=SubscriptionEventSource.TELEGRAM_EVENT,
    )
    await db_session.commit()
