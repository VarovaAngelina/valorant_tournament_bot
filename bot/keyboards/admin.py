# bot/keyboards/admin.py
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup

def get_admin_panel(role: str, tournament_status: str = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    # Если зашел разработчик — выводим абсолютно все кнопки сразу для стресс-теста
    if role == "developer":
        builder.button(text="➕ Создать турнир", callback_data="admin_create_t")
        builder.button(text="📜 Управление регламентом", callback_data="admin_rules_menu")
        builder.button(text="🎲 Провести отбор (N+M)", callback_data="admin_run_selection")
        builder.button(text="✉️ Рассылка подтверждений", callback_data="admin_send_confirmations")
        builder.button(text="🧩 Сформировать группы/команды", callback_data="admin_build_teams")
        builder.button(text="🔑 Разослать коды", callback_data="admin_send_codes")
        builder.button(text="✍️ Ввод результатов матча", callback_data="admin_enter_results")
        builder.button(text="🔄 Замена (Резерв)", callback_data="admin_make_replacement")
        builder.button(text="🏆 Сформировать финал", callback_data="admin_start_final")
        builder.button(text="📈 Экспорт в Excel", callback_data="admin_export_xlsx")
        builder.button(text="👑 Управление админами", callback_data="dev_manage_admins")
        builder.button(text="🗑️ Чистка архивов", callback_data="dev_clear_history")
    else:
        # Обычный админ видит кнопки строго по фазам (код из предыдущего шага)
        if not tournament_status or tournament_status == "draft":
            builder.button(text="➕ Создать турнир", callback_data="admin_create_t")
            builder.button(text="📜 Управление регламентом", callback_data="admin_rules_menu")
        elif tournament_status == "registration_open":
            builder.button(text="🎲 Провести отбор", callback_data="admin_run_selection")
        elif tournament_status == "selection_done":
            builder.button(text="✉️ Рассылка подтверждений", callback_data="admin_send_confirmations")
            builder.button(text="🔄 Замена (Резерв)", callback_data="admin_make_replacement")
        elif tournament_status in ["groups_formed", "stage_in_progress"]:
            builder.button(text="🔑 Разослать коды", callback_data="admin_send_codes")
            builder.button(text="✍️ Ввод результатов матча", callback_data="admin_enter_results")
        elif tournament_status == "rating_calculated":
            builder.button(text="🏆 Сформировать финал", callback_data="admin_start_final")
        elif tournament_status == "completed":
            builder.button(text="📈 Экспорт в Excel", callback_data="admin_export_xlsx")

    builder.adjust(2)
    return builder.as_markup()