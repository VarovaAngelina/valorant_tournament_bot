from aiogram.fsm.state import State, StatesGroup


class StageAdminStates(StatesGroup):
    waiting_for_match_code = State()
    waiting_for_points_win = State()
    waiting_for_points_mvp = State()
    waiting_for_scoreboard_screenshot = State()
    waiting_for_player_stats = State()
    waiting_for_edit_cell_value = State()
