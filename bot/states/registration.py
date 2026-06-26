# bot/states/registration.py
from aiogram.fsm.state import State, StatesGroup

class RegistrationStates(StatesGroup):
    waiting_for_riot_id = State()     # Ожидание Riot ID (Nickname#TAG)
    waiting_for_rank = State()         # Ожидание выбора основного ранга
    waiting_for_rank_tier = State()    # Ожидание выбора ступени (1, 2, 3)