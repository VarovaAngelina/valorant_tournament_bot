TEST_BOT_TELEGRAM_ID_BASE = 1_000_000
TEST_BOT_SLOT_MAX = 25


def is_test_bot_telegram_id(telegram_id: int) -> bool:
    """Synthetic players created by the developer «Залить 25 ботов» action."""
    if telegram_id < TEST_BOT_TELEGRAM_ID_BASE:
        return False
    slot = (telegram_id - TEST_BOT_TELEGRAM_ID_BASE) % 100
    return 1 <= slot <= TEST_BOT_SLOT_MAX


def is_test_bot_username(username: str | None) -> bool:
    if not username:
        return False
    return username.lstrip("@").startswith("bot_player_")


def is_test_bot_user(*, telegram_id: int, username: str | None = None) -> bool:
    return is_test_bot_telegram_id(telegram_id) or is_test_bot_username(username)
