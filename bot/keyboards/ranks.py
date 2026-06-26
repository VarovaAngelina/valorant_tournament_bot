from aiogram import types

RANKS = ["Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond", "Ascendant", "Immortal", "Radiant"]


def get_ranks_keyboard(*, callback_prefix: str = "rank") -> types.InlineKeyboardMarkup:
    buttons: list[list[types.InlineKeyboardButton]] = []
    row: list[types.InlineKeyboardButton] = []
    for rank in RANKS:
        row.append(
            types.InlineKeyboardButton(
                text=rank,
                callback_data=f"{callback_prefix}_{rank.lower()}",
            )
        )
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


def get_tiers_keyboard(rank_name: str, *, callback_prefix: str = "tier") -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[
            types.InlineKeyboardButton(
                text="1",
                callback_data=f"{callback_prefix}_{rank_name}_1",
            ),
            types.InlineKeyboardButton(
                text="2",
                callback_data=f"{callback_prefix}_{rank_name}_2",
            ),
            types.InlineKeyboardButton(
                text="3",
                callback_data=f"{callback_prefix}_{rank_name}_3",
            ),
        ]]
    )
