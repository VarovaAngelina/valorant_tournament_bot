from decimal import Decimal, ROUND_HALF_UP


def format_kd_ratio(kills: int | None, deaths: int | None) -> str:
    k = kills or 0
    d = deaths or 0
    if d == 0:
        ratio = Decimal(k)
    else:
        ratio = Decimal(k) / Decimal(d)
    return f"{ratio.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def format_acs(acs) -> str:
    if acs is None:
        return "0"
    return str(int(Decimal(acs)))


def format_match_stats_short(
    kills: int | None,
    deaths: int | None,
    assists: int | None,
    acs,
) -> str:
    return (
        f"K/D: {format_kd_ratio(kills, deaths)} | "
        f"K/D/A: {kills or 0}/{deaths or 0}/{assists or 0} | "
        f"ACS: {format_acs(acs)}"
    )
