from db.models import Registration


def admin_player_label(registration: Registration) -> str:
    return f"{registration.contact_telegram} ({registration.game_nick})"


def public_player_label(registration: Registration) -> str:
    return registration.game_nick
