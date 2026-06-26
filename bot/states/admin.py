from aiogram.fsm.state import State, StatesGroup


class AdminTournamentStates(StatesGroup):
    waiting_for_title = State()
    waiting_for_rules_url = State()
    waiting_for_manual_tg_id = State()
    waiting_for_manual_riot_id = State()
    waiting_for_manual_rank = State()
