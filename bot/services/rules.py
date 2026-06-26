import re
from bot.utils.timezone import now_moscow

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from db.models import ProjectSettings

TELEGRAM_MESSAGE_URL_RE = re.compile(
    r"^https?://(?:t\.me|telegram\.me)/(?:c/\d+|[\w@+-]+)/\d+(?:\?.*)?$",
    re.IGNORECASE,
)

RULES_LINK_BUTTON_TEXT = "📜 Открыть регламент"


async def _get_or_create_project_settings(db_session: AsyncSession) -> ProjectSettings:
    setting = (
        await db_session.execute(
            select(ProjectSettings).where(ProjectSettings.id == 1)
        )
    ).scalar_one_or_none()
    if setting:
        return setting

    setting = ProjectSettings(id=1)
    db_session.add(setting)
    await db_session.flush()
    return setting


def normalize_rules_url(url: str) -> str:
    cleaned = url.strip()
    if not TELEGRAM_MESSAGE_URL_RE.match(cleaned):
        raise ValueError("invalid_rules_url")
    return cleaned


def extract_rules_url(text: str) -> str:
    stripped = text.strip()
    if TELEGRAM_MESSAGE_URL_RE.match(stripped):
        return normalize_rules_url(stripped)

    for token in stripped.split():
        candidate = token.strip("<>")
        if TELEGRAM_MESSAGE_URL_RE.match(candidate):
            return normalize_rules_url(candidate)

    raise ValueError("invalid_rules_url")


async def get_global_rules_url(db_session: AsyncSession) -> str | None:
    setting = (
        await db_session.execute(
            select(ProjectSettings).where(ProjectSettings.id == 1)
        )
    ).scalar_one_or_none()
    if setting and setting.rules_url and setting.rules_url.strip():
        return setting.rules_url.strip()
    return None


async def set_global_rules_url(db_session: AsyncSession, rules_url: str) -> None:
    normalized = normalize_rules_url(rules_url)
    setting = await _get_or_create_project_settings(db_session)
    setting.rules_url = normalized
    setting.updated_at = now_moscow()


async def get_tournament_rules_url(db_session: AsyncSession, tour_id: int) -> str | None:
    _ = tour_id
    return await get_global_rules_url(db_session)


def rules_link_keyboard(
    rules_url: str,
    *,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=RULES_LINK_BUTTON_TEXT, url=rules_url)]
    ]
    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rules_registration_keyboard(tour_id: int, rules_url: str) -> InlineKeyboardMarkup:
    return rules_link_keyboard(
        rules_url,
        extra_rows=[[
            InlineKeyboardButton(
                text="✅ Я ознакомился с регламентом и принимаю его условия",
                callback_data=f"rules_accept_{tour_id}",
            )
        ]],
    )


def admin_rules_menu_keyboard(rules_url: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if rules_url:
        rows.append([
            InlineKeyboardButton(text="👁 Просмотр регламента", url=rules_url),
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                text="👁 Просмотр регламента",
                callback_data="admin_rules_view_missing",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="✏️ Редактирование регламента",
            callback_data="admin_rules_edit_link",
        )
    ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_home"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
