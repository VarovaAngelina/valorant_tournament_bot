from bot.utils.timezone import now_moscow

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from config import settings
from bot.utils.test_bots import is_test_bot_telegram_id
from db.models import (
    Admin,
    AdminStatus,
    Registration,
    RegistrationStatus,
    SubscriptionEvent,
    SubscriptionEventSource,
    SubscriptionEventType,
    SubscriptionStatus,
    Tournament,
    TournamentStatus,
    User,
)

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
)

SUBSCRIBED_MEMBER_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
}
if hasattr(ChatMemberStatus, "OWNER"):
    SUBSCRIBED_MEMBER_STATUSES.add(ChatMemberStatus.OWNER)


def normalize_channel_username(username: str | None) -> str | None:
    if not username:
        return None
    username = username.strip()
    if not username:
        return None
    return username if username.startswith("@") else f"@{username}"


def build_channel_chat_candidates(
    channel_id: int,
    channel_username: str | None = None,
) -> list[int | str]:
    candidates: list[int | str] = []
    seen: set[str | int] = set()

    def add(value: int | str) -> None:
        if value in seen:
            return
        seen.add(value)
        candidates.append(value)

    add(channel_id)
    if channel_id > 0:
        add(int(f"-100{channel_id}"))
    elif channel_id < 0 and not str(channel_id).startswith("-100"):
        add(int(f"-100{abs(channel_id)}"))

    username = normalize_channel_username(channel_username)
    if username:
        add(username)

    env_username = normalize_channel_username(getattr(settings, "CHANNEL_USERNAME", None))
    if env_username:
        add(env_username)

    if settings.CHANNEL_ID != channel_id:
        add(settings.CHANNEL_ID)
        if settings.CHANNEL_ID > 0:
            add(int(f"-100{settings.CHANNEL_ID}"))

    return candidates


def member_is_subscribed(member) -> bool:
    if isinstance(member, (ChatMemberMember, ChatMemberAdministrator, ChatMemberOwner)):
        return True
    if isinstance(member, ChatMemberRestricted):
        return bool(member.is_member)
    status = getattr(member, "status", None)
    if status in SUBSCRIBED_MEMBER_STATUSES:
        return True
    if status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


async def resolve_channel_username(bot: Bot, channel_id: int) -> str | None:
    configured = normalize_channel_username(getattr(settings, "CHANNEL_USERNAME", None))
    if configured:
        return configured
    try:
        chat = await bot.get_chat(channel_id)
        if chat.username:
            return f"@{chat.username}"
    except Exception as exc:
        logger.warning(f"Could not resolve channel username for {channel_id}: {exc}")
    return None


async def get_channel_open_link(
    bot: Bot,
    channel_id: int,
    channel_username: str | None = None,
) -> str | None:
    username = normalize_channel_username(channel_username)
    if not username:
        username = await resolve_channel_username(bot, channel_id)
    if username:
        return f"https://t.me/{username.lstrip('@')}"

    for chat_id in build_channel_chat_candidates(channel_id, channel_username):
        try:
            chat = await bot.get_chat(chat_id)
            if chat.username:
                return f"https://t.me/{chat.username}"
            if chat.invite_link:
                return chat.invite_link
        except Exception:
            continue
    return None


async def check_user_subscription(
    bot: Bot,
    channel_id: int,
    telegram_id: int,
    *,
    channel_username: str | None = None,
) -> tuple[bool, str | None]:
    """Return (is_subscribed, error_code).

    error_code is None on success.
    error_code == 'not_subscribed' when API answered and user is not a member.
    error_code == 'check_failed' when bot could not verify (permissions/config/network).
    """
    api_errors: list[str] = []
    saw_member = False

    for chat_id in build_channel_chat_candidates(channel_id, channel_username):
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=telegram_id)
            saw_member = True
            if member_is_subscribed(member):
                return True, None
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            api_errors.append(f"{chat_id}: {exc}")
            logger.warning(
                f"Subscription check failed for user {telegram_id} in chat {chat_id}: {exc}"
            )
        except Exception as exc:
            api_errors.append(f"{chat_id}: {exc}")
            logger.exception(
                f"Unexpected subscription check error for user {telegram_id} in chat {chat_id}"
            )

    if saw_member:
        return False, "not_subscribed"

    if api_errors:
        logger.error(
            "Subscription verification unavailable. "
            f"Ensure the bot is an administrator in the tournament channel. Details: {api_errors}"
        )
        return False, "check_failed"

    return False, "not_subscribed"


async def is_user_subscribed(
    bot: Bot,
    channel_id: int,
    telegram_id: int,
    *,
    channel_username: str | None = None,
) -> bool:
    subscribed, _ = await check_user_subscription(
        bot,
        channel_id,
        telegram_id,
        channel_username=channel_username,
    )
    return subscribed


def subscription_check_keyboard(
    tour_id: int,
    *,
    channel_url: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if channel_url:
        rows.append([InlineKeyboardButton(text="📢 Открыть канал", url=channel_url)])
    rows.append([
        InlineKeyboardButton(
            text="✅ Я подписался, проверить ещё раз",
            callback_data=f"sub_check_{tour_id}",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_subscription_required_message(
    bot: Bot,
    tour: Tournament,
    *,
    error_code: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    channel_username = tour.channel_username or await resolve_channel_username(bot, tour.channel_id)
    channel_hint = channel_username or "канал турнира"
    channel_url = await get_channel_open_link(bot, tour.channel_id, channel_username)

    if error_code == "check_failed":
        text = (
            f"⚠️ Не удалось проверить подписку на {channel_hint}.\n\n"
            "Если вы уже подписаны, попросите администратора убедиться, что бот добавлен "
            "в канал как администратор (иначе Telegram не даёт проверить подписчиков).\n\n"
            "После этого нажмите кнопку проверки ещё раз."
        )
    elif error_code == "not_subscribed":
        text = (
            f"❌ Подписка на {channel_hint} не найдена.\n\n"
            "Подпишитесь на канал и нажмите «Я подписался, проверить ещё раз»."
        )
    else:
        text = (
            f"📢 Для регистрации нужна подписка на {channel_hint}.\n"
            "Подпишитесь на канал и нажмите кнопку проверки."
        )

    return text, subscription_check_keyboard(tour.id, channel_url=channel_url)


async def _log_event(
    db_session: AsyncSession,
    registration_id: int,
    event_type: SubscriptionEventType,
    source: SubscriptionEventSource,
) -> None:
    db_session.add(
        SubscriptionEvent(
            registration_id=registration_id,
            event_type=event_type,
            source=source,
        )
    )


async def _notify_admins(bot: Bot, db_session: AsyncSession, text: str, tour_id: int) -> None:
    admin_ids = set(
        (
            await db_session.execute(
                select(Admin.telegram_id).where(Admin.admin_status == AdminStatus.ACTIVE)
            )
        ).scalars().all()
    )
    admin_ids.add(settings.DEVELOPER_TG_ID)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="👥 Управление турниром",
                callback_data=f"manage_tour_{tour_id}",
            )
        ]]
    )
    for telegram_id in admin_ids:
        try:
            await bot.send_message(telegram_id, text, reply_markup=keyboard)
        except Exception:
            pass


async def get_active_registration_for_user(
    db_session: AsyncSession,
    telegram_id: int,
) -> tuple[Tournament, Registration] | None:
    row = (
        await db_session.execute(
            select(Tournament, Registration)
            .join(Registration, Registration.tournament_id == Tournament.id)
            .join(User, User.id == Registration.user_id)
            .where(
                User.telegram_id == telegram_id,
                Tournament.status.in_(ACTIVE_TOURNAMENT_STATUSES),
                Registration.status.not_in(
                    (
                        RegistrationStatus.WITHDRAWN,
                        RegistrationStatus.EXCLUDED,
                        RegistrationStatus.NOT_SELECTED,
                    )
                ),
            )
            .order_by(Registration.id.desc())
            .limit(1)
        )
    ).first()
    if not row:
        return None
    return row[0], row[1]


async def sync_registration_subscription(
    db_session: AsyncSession,
    bot: Bot,
    registration: Registration,
    tour: Tournament,
    *,
    source: SubscriptionEventSource,
) -> bool:
    user_tg = (
        await db_session.execute(select(User.telegram_id).where(User.id == registration.user_id))
    ).scalar_one()

    if is_test_bot_telegram_id(user_tg):
        if registration.subscription_status != SubscriptionStatus.SUBSCRIBED:
            registration.subscription_status = SubscriptionStatus.SUBSCRIBED
            registration.unsubscribed_at = None
        return True

    subscribed, error_code = await check_user_subscription(
        bot,
        tour.channel_id,
        user_tg,
        channel_username=tour.channel_username,
    )
    if error_code == "check_failed":
        return registration.subscription_status == SubscriptionStatus.SUBSCRIBED

    if subscribed:
        if registration.subscription_status != SubscriptionStatus.SUBSCRIBED:
            registration.subscription_status = SubscriptionStatus.SUBSCRIBED
            registration.unsubscribed_at = None
            await _log_event(db_session, registration.id, SubscriptionEventType.SUBSCRIBED, source)
            try:
                await bot.send_message(
                    user_tg,
                    f"✅ Подписка на канал турнира «{tour.title}» восстановлена.\n"
                    "Вы снова участвуете в турнире.",
                )
            except Exception:
                pass
            await _notify_admins(
                bot,
                db_session,
                f"ℹ️ Участник {registration.contact_telegram} ({registration.game_nick}) "
                f"снова подписался на канал турнира «{tour.title}».",
                tour.id,
            )
        return True

    if registration.subscription_status == SubscriptionStatus.UNSUBSCRIBED:
        return False

    registration.subscription_status = SubscriptionStatus.UNSUBSCRIBED
    registration.unsubscribed_at = now_moscow()
    registration.participation_confirmed = False
    registration.participation_confirmed_at = None
    await _log_event(db_session, registration.id, SubscriptionEventType.UNSUBSCRIBED, source)

    channel_url = await get_channel_open_link(bot, tour.channel_id, tour.channel_username)
    try:
        await bot.send_message(
            user_tg,
            f"⚠️ Вы отписались от канала турнира «{tour.title}».\n"
            "Без активной подписки участие невозможно. Подпишитесь снова — статус восстановится автоматически.\n"
            "Администратор также может исключить вас из состава вручную.",
            reply_markup=subscription_check_keyboard(tour.id, channel_url=channel_url),
        )
    except Exception:
        pass
    await _notify_admins(
        bot,
        db_session,
        f"⚠️ Участник {registration.contact_telegram} ({registration.game_nick}) "
        f"отписался от канала турнира «{tour.title}».\n"
        "Подтверждение участия сброшено. Замену нужно выполнить вручную.",
        tour.id,
    )
    return False


async def run_scheduled_subscription_check(bot: Bot, db_session: AsyncSession) -> int:
    rows = (
        await db_session.execute(
            select(Tournament, Registration)
            .join(Registration, Registration.tournament_id == Tournament.id)
            .where(
                Tournament.status.in_(ACTIVE_TOURNAMENT_STATUSES),
                Registration.status.in_(
                    (
                        RegistrationStatus.REGISTERED,
                        RegistrationStatus.SELECTED_MAIN,
                        RegistrationStatus.SELECTED_RESERVE,
                    )
                ),
            )
        )
    ).all()
    changed = 0
    for tour, registration in rows:
        before = registration.subscription_status
        await sync_registration_subscription(
            db_session,
            bot,
            registration,
            tour,
            source=SubscriptionEventSource.SCHEDULED_CHECK,
        )
        if registration.subscription_status != before:
            changed += 1
    if changed:
        await db_session.commit()
    return changed
