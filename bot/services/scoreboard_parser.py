import base64
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from difflib import get_close_matches

from loguru import logger

from bot.services.scoring import PlayerMatchStats
from config import settings
from db.models import Registration


def _vision_http_client_kwargs() -> dict:
    kwargs: dict = {"timeout": 60.0}
    if settings.HTTP_PROXY:
        kwargs["proxy"] = settings.HTTP_PROXY
    return kwargs


KDA_PATTERN = re.compile(
    r"(?P<k>\d+)\s*[/\\|lI1]\s*(?P<d>\d+)\s*[/\\|lI1]\s*(?P<a>\d+)",
    re.IGNORECASE,
)
ACS_BEFORE_KDA = re.compile(
    r"(?P<acs>\d{2,4})\s+(?P<k>\d+)\s*[/\\|lI1]\s*(?P<d>\d+)\s*[/\\|lI1]\s*(?P<a>\d+)",
    re.IGNORECASE,
)
ROW_PATTERN = re.compile(
    r"(?P<acs>\d{2,4})\s+"
    r"(?P<k>\d+)\s*/\s*(?P<d>\d+)\s*/\s*(?P<a>\d+)\s+"
    r"(?P<econ>\d+)\s+(?P<fb>\d+)\s+(?P<plant>\d+)\s+(?P<defuse>\d+)\s*$"
)
ROW_PATTERN_SPACES = re.compile(
    r"(?P<acs>\d{2,4})\s+"
    r"(?P<k>\d+)\s+(?P<d>\d+)\s+(?P<a>\d+)\s+"
    r"(?P<econ>\d+)\s+(?P<fb>\d+)\s+(?P<plant>\d+)\s+(?P<defuse>\d+)\s*$"
)

VALORANT_AGENTS = {
    "JETT", "RAZE", "BREACH", "OMEN", "BRIMSTONE", "PHOENIX", "SAGE", "SOVA",
    "VIPER", "CYPHER", "REYNA", "KILLJOY", "SKYE", "YORU", "ASTRA", "KAYO",
    "KAY/O", "CHAMBER", "NEON", "FADE", "HARBOR", "GEKKO", "DEADLOCK", "ISO",
    "CLOVE", "VYSE", "TEJO", "WAYLAY", "KILYOY", "KILLVOY",
}
HEADER_WORDS = {
    "ИНДИВИДУАЛЬНАЯ", "СОРТИРОВКА", "СРЕДНИЙ", "СЧЕТ", "МАТЧА", "УСП", "ЭКОНОМ",
    "ПЕРВАЯ", "КРОВЬ", "ЗАЛОЖЕНО", "SPIKE", "ОБЕЗВРЕЖЕНО", "ПОРАЖЕНИЕ", "ПОБЕДА",
    "INDIVIDUAL", "SCOREBOARD", "COMBAT", "ECON", "FIRST", "BLOOD", "PLANTED",
    "DEFUSED", "SORT", "AVERAGE", "MATCH", "SCORE", "ТАБЛИЦА",
}

SCOREBOARD_VISION_PROMPT = (
    "На скриншоте таблица результатов матча Valorant после игры. "
    "Извлеки всех 10 игроков. Для каждого верни объект JSON с полями: "
    "name, acs, kills, deaths, assists, econ, first_bloods, spikes_planted, spikes_defused. "
    "В name только ник игрока без имени агента. "
    "Ответь только JSON-массивом без markdown."
)


@dataclass
class ParsedScoreboardRow:
    raw_name: str
    stats: PlayerMatchStats


@dataclass
class StatBlock:
    position: int
    stats: PlayerMatchStats


@dataclass
class ScoreboardMatchResult:
    matched: dict[int, PlayerMatchStats]
    unmatched_rows: list[ParsedScoreboardRow]
    unmatched_players: list[Registration]


def _normalize_name(value: str) -> str:
    value = value.split("(")[0].strip()
    value = re.sub(r"[^a-z0-9#]", "", value.lower())
    return value


def _normalize_search_text(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", value.lower())


def _registration_aliases(registration: Registration) -> list[str]:
    aliases = [_normalize_name(registration.game_nick)]
    if "#" in registration.game_nick:
        aliases.append(_normalize_name(registration.game_nick.split("#", 1)[0]))
    return [item for item in aliases if item]


def _nick_search_patterns(game_nick: str) -> list[str]:
    base = game_nick.split("#", 1)[0]
    patterns = [base]
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", base)
    if spaced != base:
        patterns.append(spaced)
    compact = re.sub(r"[^a-zA-Z0-9]", "", base)
    if compact:
        patterns.append(compact)
        if len(compact) >= 5:
            patterns.append(compact[:5])
        if len(compact) >= 4:
            patterns.append(compact[:4])
    return list(dict.fromkeys(patterns))


def _find_nick_positions(text: str, pattern: str) -> list[int]:
    positions: list[int] = []
    targets = {pattern.lower(), _normalize_search_text(pattern).strip()}
    compact = re.sub(r"[^a-zA-Z0-9]", "", pattern).lower()
    if len(compact) >= 4:
        targets.add(compact)

    haystacks = (
        text.lower(),
        _normalize_search_text(text),
    )
    for target in targets:
        if len(target) < 3:
            continue
        for haystack in haystacks:
            start = 0
            while True:
                index = haystack.find(target, start)
                if index == -1:
                    break
                positions.append(index)
                start = index + max(len(target) // 2, 1)
    return sorted(set(positions))


def _stats_from_match(match: re.Match) -> PlayerMatchStats:
    return PlayerMatchStats(
        acs=Decimal(match.group("acs")),
        kills=int(match.group("k")),
        deaths=int(match.group("d")),
        assists=int(match.group("a")),
        econ_rating=int(match.group("econ")),
        first_bloods=int(match.group("fb")),
        spikes_planted=int(match.group("plant")),
        spikes_defused=int(match.group("defuse")),
    )


def _tail_numbers(after_kda: str) -> tuple[int, int, int, int]:
    numbers = [int(item) for item in re.findall(r"\d+", after_kda[:24])]
    econ = numbers[0] if numbers else 0
    fb = numbers[1] if len(numbers) > 1 else 0
    plant = numbers[2] if len(numbers) > 2 else 0
    defuse = numbers[3] if len(numbers) > 3 else 0
    return econ, fb, plant, defuse


def _pick_acs(before_kda: str) -> str | None:
    trimmed = before_kda.strip()
    immediate = re.search(r"(?<!\d)(\d{2,4})(?!\d)\s*$", trimmed)
    if immediate:
        value = int(immediate.group(1))
        if 80 <= value <= 500:
            return immediate.group(1)
    return None


def _find_acs_for_kda(text: str, kda_start: int) -> str | None:
    before = text[max(0, kda_start - 120) : kda_start]
    immediate = _pick_acs(before)
    if immediate:
        return immediate

    last_kda_end = 0
    for match in KDA_PATTERN.finditer(before):
        last_kda_end = match.end()
    tail = before[last_kda_end:]
    for num in reversed(re.findall(r"(?<!\d)(\d{2,4})(?!\d)", tail)):
        value = int(num)
        if 80 <= value <= 500:
            return num
    return None


def _stats_look_valid(stats: PlayerMatchStats) -> bool:
    acs = int(stats.acs)
    if not (50 <= acs <= 550):
        return False
    if stats.kills == acs or stats.deaths == acs or stats.assists == acs:
        return False
    if not (0 <= stats.kills <= 40 and 0 <= stats.deaths <= 40 and 0 <= stats.assists <= 40):
        return False
    if not (0 <= stats.econ_rating <= 100):
        return False
    return True


def _parse_from_acs_kda_match(match: re.Match, text: str) -> PlayerMatchStats | None:
    econ, fb, plant, defuse = _tail_numbers(text[match.end() : match.end() + 24])
    stats = PlayerMatchStats(
        acs=Decimal(match.group("acs")),
        kills=int(match.group("k")),
        deaths=int(match.group("d")),
        assists=int(match.group("a")),
        econ_rating=econ,
        first_bloods=fb,
        spikes_planted=plant,
        spikes_defused=defuse,
    )
    return stats if _stats_look_valid(stats) else None


def _parse_stat_block_at(text: str, kda_match: re.Match) -> PlayerMatchStats | None:
    acs_match = ACS_BEFORE_KDA.search(text, max(0, kda_match.start() - 8), kda_match.end())
    if acs_match and acs_match.start() <= kda_match.start() <= acs_match.end():
        return _parse_from_acs_kda_match(acs_match, text)

    acs = _find_acs_for_kda(text, kda_match.start())
    if not acs:
        return None

    econ, fb, plant, defuse = _tail_numbers(text[kda_match.end() : kda_match.end() + 24])
    stats = PlayerMatchStats(
        acs=Decimal(acs),
        kills=int(kda_match.group("k")),
        deaths=int(kda_match.group("d")),
        assists=int(kda_match.group("a")),
        econ_rating=econ,
        first_bloods=fb,
        spikes_planted=plant,
        spikes_defused=defuse,
    )
    return stats if _stats_look_valid(stats) else None


def _find_stat_blocks(text: str) -> list[StatBlock]:
    compact = re.sub(r"\s+", " ", text)
    blocks: list[StatBlock] = []
    seen: set[tuple[int, int, int, int]] = set()
    covered_kda: set[tuple[int, int, int]] = set()

    for acs_match in ACS_BEFORE_KDA.finditer(compact):
        stats = _parse_from_acs_kda_match(acs_match, compact)
        if not stats:
            continue
        signature = (int(stats.acs), stats.kills, stats.deaths, stats.assists)
        if signature in seen:
            continue
        seen.add(signature)
        covered_kda.add((stats.kills, stats.deaths, stats.assists))
        blocks.append(StatBlock(position=acs_match.start(), stats=stats))

    for kda_match in KDA_PATTERN.finditer(compact):
        kda_key = (
            int(kda_match.group("k")),
            int(kda_match.group("d")),
            int(kda_match.group("a")),
        )
        if kda_key in covered_kda:
            continue
        stats = _parse_stat_block_at(compact, kda_match)
        if not stats:
            continue
        signature = (int(stats.acs), stats.kills, stats.deaths, stats.assists)
        if signature in seen:
            continue
        seen.add(signature)
        covered_kda.add(kda_key)
        blocks.append(StatBlock(position=kda_match.start(), stats=stats))

    blocks.sort(key=lambda item: item.position)
    return blocks


def _closest_registration_for_block(
    block: StatBlock,
    registrations: list[Registration],
    text: str,
) -> tuple[Registration | None, int]:
    closest_registration: Registration | None = None
    closest_distance = 10_000

    for registration in registrations:
        for pattern in _nick_search_patterns(registration.game_nick):
            for nick_pos in _find_nick_positions(text, pattern):
                distance = abs(nick_pos - block.position)
                if distance < closest_distance:
                    closest_distance = distance
                    closest_registration = registration

    return closest_registration, closest_distance


def _match_players_from_ocr_text(
    text: str,
    registrations: list[Registration],
) -> dict[int, PlayerMatchStats]:
    blocks = _find_stat_blocks(text)
    if not blocks:
        return {}

    candidates: list[tuple[int, int, Registration, StatBlock]] = []
    for block_index, block in enumerate(blocks):
        registration, distance = _closest_registration_for_block(block, registrations, text)
        if registration is None or distance > 280:
            continue
        candidates.append((distance, block_index, registration, block))

    candidates.sort(key=lambda item: (item[0], item[1]))
    matched: dict[int, PlayerMatchStats] = {}
    used_blocks: set[int] = set()

    for distance, block_index, registration, block in candidates:
        if block_index in used_blocks or registration.id in matched:
            continue
        owner, owner_distance = _closest_registration_for_block(block, registrations, text)
        if owner is None or owner.id != registration.id:
            continue
        matched[registration.id] = block.stats
        used_blocks.add(block_index)

    return matched


def _row_buckets_from_data(data: dict, y_tolerance: int = 14) -> dict[int, list[tuple[int, str]]]:
    rows: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for index, text in enumerate(data["text"]):
        token = text.strip()
        if not token:
            continue
        try:
            confidence = int(float(data["conf"][index]))
        except (TypeError, ValueError):
            confidence = -1
        if confidence >= 0 and confidence < 15:
            continue
        top = int(data["top"][index])
        height = max(int(data["height"][index]), 1)
        y_center = top + height // 2
        bucket = y_center // y_tolerance
        rows[bucket].append((int(data["left"][index]), token))
    return rows


def _line_from_bucket(parts: list[tuple[int, str]]) -> str:
    return " ".join(token for _, token in sorted(parts, key=lambda item: item[0])).strip()


def _match_players_from_column_ocr(
    image_bytes: bytes,
    registrations: list[Registration],
) -> dict[int, PlayerMatchStats]:
    import pytesseract

    from PIL import Image, ImageEnhance, ImageOps
    import io

    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    scale = 3 if max(width, height) < 1600 else 2
    image = image.resize((width * scale, height * scale), Image.Resampling.LANCZOS)
    width, height = image.size

    top = int(height * 0.18)
    bottom = int(height * 0.96)
    matched: dict[int, PlayerMatchStats] = {}

    for name_ratio, stats_ratio in ((0.36, 0.30), (0.40, 0.28), (0.32, 0.34)):
        names_image = image.crop((0, top, int(width * name_ratio), bottom))
        stats_image = image.crop((int(width * stats_ratio), top, width, bottom))

        names_gray = ImageOps.grayscale(names_image)
        names_gray = ImageEnhance.Contrast(names_gray).enhance(2.0)

        stats_gray = ImageOps.grayscale(stats_image)
        stats_gray = ImageEnhance.Contrast(stats_gray).enhance(2.5)
        stats_gray = stats_gray.point(lambda pixel: 255 if pixel > 130 else 0)

        names_data = pytesseract.image_to_data(
            names_gray,
            lang="eng+rus",
            output_type=pytesseract.Output.DICT,
            config="--psm 6",
        )
        stats_data = pytesseract.image_to_data(
            stats_gray,
            output_type=pytesseract.Output.DICT,
            config="--psm 6 -c tessedit_char_whitelist=0123456789/ ",
        )

        name_rows = _row_buckets_from_data(names_data)
        stat_rows = _row_buckets_from_data(stats_data, y_tolerance=12)
        if not stat_rows:
            continue

        used_stat_buckets: set[int] = set()
        for stat_bucket in sorted(stat_rows.keys()):
            stat_line = _line_from_bucket(stat_rows[stat_bucket])
            kda_match = KDA_PATTERN.search(stat_line)
            if not kda_match:
                acs_match = ACS_BEFORE_KDA.search(stat_line)
                if not acs_match:
                    continue
                stats = _parse_from_acs_kda_match(acs_match, stat_line)
            else:
                stats = _parse_stat_block_at(stat_line, kda_match)
            if not stats:
                continue

            closest_name_bucket: int | None = None
            closest_distance = 10_000
            for name_bucket in name_rows.keys():
                distance = abs(name_bucket - stat_bucket)
                if distance < closest_distance:
                    closest_distance = distance
                    closest_name_bucket = name_bucket

            if closest_name_bucket is None or closest_distance > 5:
                continue

            name_line = _clean_player_name(_line_from_bucket(name_rows[closest_name_bucket]))
            if len(name_line) < 2:
                continue

            row_key = _normalize_name(name_line)
            registration = None
            for item in registrations:
                if item.id in matched:
                    continue
                aliases = _registration_aliases(item)
                if row_key in aliases:
                    registration = item
                    break
                close = get_close_matches(row_key, aliases, n=1, cutoff=0.72)
                if close:
                    registration = item
                    break

            if registration is None:
                continue

            matched[registration.id] = stats
            used_stat_buckets.add(stat_bucket)

        if len(matched) >= len(registrations):
            break

    return matched


def _clean_player_name(name: str) -> str:
    name = re.sub(r"\s{2,}", " ", name.strip(" -•|"))
    tokens = name.split()
    filtered: list[str] = []
    for token in tokens:
        upper = re.sub(r"[^A-Z/]", "", token.upper())
        if upper in VALORANT_AGENTS or upper.replace("/", "") in VALORANT_AGENTS:
            continue
        if upper in HEADER_WORDS:
            continue
        filtered.append(token)
    cleaned = " ".join(filtered).strip()
    return cleaned if len(cleaned) >= 2 else name.strip()


def _line_has_stats(line: str) -> bool:
    return bool(KDA_PATTERN.search(line))


def _looks_like_header(line: str) -> bool:
    upper = line.upper()
    hits = sum(1 for word in HEADER_WORDS if word in upper)
    return hits >= 2 or "ПОРАЖЕНИЕ" in upper or "ПОБЕДА" in upper


def _parse_stats_line(line: str) -> tuple[str, PlayerMatchStats] | None:
    line = line.strip()
    if not line or _looks_like_header(line):
        return None

    match = ROW_PATTERN.search(line) or ROW_PATTERN_SPACES.search(line)
    if match:
        name = _clean_player_name(line[: match.start()])
        if len(name) >= 2:
            return name, _stats_from_match(match)

    kda_match = KDA_PATTERN.search(line)
    if not kda_match:
        return None

    stats = _parse_stat_block_at(line, kda_match)
    if not stats:
        return None

    name = _clean_player_name(line[: kda_match.start()])
    if len(name) < 2:
        return None
    return name, stats


def _merge_pending_name_lines(lines: list[str]) -> list[str]:
    merged: list[str] = []
    pending_name: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _looks_like_header(stripped):
            pending_name = []
            continue

        if _line_has_stats(stripped):
            if pending_name:
                name_part = " ".join(pending_name)
                merged.append(f"{name_part} {stripped}")
                pending_name = []
            else:
                merged.append(stripped)
            continue

        if re.search(r"[A-Za-zА-Яа-я]", stripped) and not re.fullmatch(r"[\d\s/\\|]+", stripped):
            pending_name.append(stripped)
        else:
            pending_name = []

    return merged


def _extract_rows_from_text(text: str) -> list[ParsedScoreboardRow]:
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidate_lines = _merge_pending_name_lines(raw_lines)

    rows: list[ParsedScoreboardRow] = []
    seen_stats: set[tuple[int, int, int, int]] = set()

    for line in candidate_lines:
        parsed = _parse_stats_line(line)
        if not parsed:
            continue
        name, stats = parsed
        signature = (int(stats.acs), stats.kills, stats.deaths, stats.assists)
        if signature in seen_stats:
            continue
        seen_stats.add(signature)
        rows.append(ParsedScoreboardRow(raw_name=name, stats=stats))

    if len(rows) >= 8:
        return rows

    for block in _find_stat_blocks(text):
        signature = (
            int(block.stats.acs),
            block.stats.kills,
            block.stats.deaths,
            block.stats.assists,
        )
        if signature in seen_stats:
            continue
        seen_stats.add(signature)
        rows.append(ParsedScoreboardRow(raw_name="", stats=block.stats))

    return rows


def _lines_from_tesseract_data(data: dict) -> list[str]:
    buckets: dict[int, list[tuple[int, str]]] = defaultdict(list)
    count = len(data["text"])
    for index in range(count):
        text = data["text"][index].strip()
        if not text:
            continue
        try:
            confidence = int(float(data["conf"][index]))
        except (TypeError, ValueError):
            confidence = -1
        if confidence >= 0 and confidence < 20:
            continue
        height = max(int(data["height"][index]), 1)
        y_bucket = (int(data["top"][index]) + height // 2) // max(height // 2, 8)
        buckets[y_bucket].append((int(data["left"][index]), text))

    lines: list[str] = []
    for y_bucket in sorted(buckets.keys()):
        parts = [text for _, text in sorted(buckets[y_bucket], key=lambda item: item[0])]
        line = " ".join(parts).strip()
        if line:
            lines.append(line)
    return lines


def _preprocess_variants(image_bytes: bytes):
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    import io

    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    scale = 3 if max(width, height) < 1600 else 2
    image = image.resize((width * scale, height * scale), Image.Resampling.LANCZOS)

    variants = [image]
    if height > 400:
        variants.append(image.crop((0, int(height * 0.12), width, height)))
        variants.append(image.crop((0, int(height * 0.18), width, int(height * 0.96))))

    processed = []
    for variant in variants:
        gray = ImageOps.grayscale(variant)
        contrast = ImageEnhance.Contrast(gray).enhance(2.0)
        sharp = contrast.filter(ImageFilter.SHARPEN)
        processed.append(sharp)
        processed.append(sharp.point(lambda pixel: 255 if pixel > 135 else 0))
    return processed


def _run_tesseract(image_bytes: bytes) -> str:
    import pytesseract

    chunks: list[str] = []
    for image in _preprocess_variants(image_bytes):
        for config in ("--psm 6", "--psm 4", "--psm 11"):
            chunks.append(pytesseract.image_to_string(image, lang="eng+rus", config=config))

        data = pytesseract.image_to_data(
            image,
            lang="eng+rus",
            output_type=pytesseract.Output.DICT,
            config="--psm 6",
        )
        chunks.append("\n".join(_lines_from_tesseract_data(data)))

    return "\n".join(chunks)


def _merge_matched(
    primary: dict[int, PlayerMatchStats],
    secondary: dict[int, PlayerMatchStats],
) -> dict[int, PlayerMatchStats]:
    merged = dict(primary)
    for reg_id, stats in secondary.items():
        merged.setdefault(reg_id, stats)
    return merged


def _parse_with_tesseract(
    image_bytes: bytes,
    registrations: list[Registration],
) -> tuple[list[ParsedScoreboardRow], dict[int, PlayerMatchStats], str]:
    text = _run_tesseract(image_bytes)
    column_matched = _match_players_from_column_ocr(image_bytes, registrations)
    text_matched = _match_players_from_ocr_text(text, registrations)
    matched_by_nick = _merge_matched(column_matched, text_matched)
    rows = _extract_rows_from_text(text)
    logger.info(
        "Tesseract OCR: column={}/{}, text={}/{}, stat_rows={}",
        len(column_matched),
        len(registrations),
        len(text_matched),
        len(registrations),
        len(rows),
    )
    return rows, matched_by_nick, text


async def _parse_with_openai_vision(image_bytes: bytes) -> list[ParsedScoreboardRow]:
    if not settings.OPENAI_API_KEY:
        return []

    import httpx

    prepared_bytes, mime_type = _prepare_vision_image(image_bytes)
    payload = {
        "model": settings.OPENAI_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": SCOREBOARD_VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{base64.b64encode(prepared_bytes).decode()}",
                        },
                    },
                ],
            }
        ],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(**_vision_http_client_kwargs()) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()

    return _parse_vision_json_content(content)


def _guess_image_mime(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _parse_vision_json_content(content: str) -> list[ParsedScoreboardRow]:
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
    data = json.loads(content)

    rows: list[ParsedScoreboardRow] = []
    for item in data:
        rows.append(
            ParsedScoreboardRow(
                raw_name=str(item["name"]),
                stats=PlayerMatchStats(
                    acs=Decimal(str(item["acs"])),
                    kills=int(item["kills"]),
                    deaths=int(item["deaths"]),
                    assists=int(item["assists"]),
                    econ_rating=int(item["econ"]),
                    first_bloods=int(item["first_bloods"]),
                    spikes_planted=int(item["spikes_planted"]),
                    spikes_defused=int(item["spikes_defused"]),
                ),
            )
        )
    return rows


def _prepare_vision_image(image_bytes: bytes, max_side: int = 1600) -> tuple[bytes, str]:
    from PIL import Image
    import io

    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    longest = max(width, height)
    if longest > max_side:
        scale = max_side / longest
        image = image.resize(
            (int(width * scale), int(height * scale)),
            Image.Resampling.LANCZOS,
        )

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def _gemini_models_to_try() -> list[str]:
    models: list[str] = []
    if settings.GEMINI_VISION_MODEL:
        models.append(settings.GEMINI_VISION_MODEL.strip())
    for fallback in (
        "gemini-2.0-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.5-flash",
    ):
        if fallback not in models:
            models.append(fallback)
    return models


def _is_valid_gemini_api_key() -> bool:
    key = (settings.GEMINI_API_KEY or "").strip()
    return bool(key) and (key.startswith("AIza") or key.startswith("AQ."))


def _validate_gemini_api_key() -> None:
    key = (settings.GEMINI_API_KEY or "").strip()
    if not key:
        return
    if key.startswith("AQ."):
        return
    if key.startswith("AIza"):
        return
    logger.warning(
        "GEMINI_API_KEY имеет необычный формат. Ожидается AQ... (новый ключ AI Studio) "
        "или AIza... (старый). Создайте ключ на https://aistudio.google.com/app/apikey"
    )


async def _call_gemini_vision(image_bytes: bytes, model: str) -> list[ParsedScoreboardRow]:
    import httpx

    prepared_bytes, mime_type = _prepare_vision_image(image_bytes)
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": SCOREBOARD_VISION_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64.b64encode(prepared_bytes).decode(),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    async with httpx.AsyncClient(**_vision_http_client_kwargs()) as client:
        response = await client.post(
            url,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": settings.GEMINI_API_KEY or "",
            },
            json=payload,
        )
        if response.is_error:
            logger.warning(
                "Gemini model {} error {}: {}",
                model,
                response.status_code,
                response.text[:500],
            )
            response.raise_for_status()
        body = response.json()

    try:
        content = body["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Unexpected Gemini response format: {body}") from exc

    return _parse_vision_json_content(content)


async def _parse_with_gemini_vision(image_bytes: bytes) -> list[ParsedScoreboardRow]:
    if not settings.GEMINI_API_KEY:
        return []
    if not _is_valid_gemini_api_key():
        raise ValueError("invalid_gemini_key")

    import httpx

    last_status: int | None = None
    last_error: Exception | None = None

    for model in _gemini_models_to_try():
        try:
            rows = await _call_gemini_vision(image_bytes, model)
            logger.info(f"Gemini model {model} extracted {len(rows)} scoreboard rows")
            return rows
        except httpx.HTTPStatusError as exc:
            last_status = exc.response.status_code
            last_error = exc
            if exc.response.status_code in {429, 404}:
                continue
            raise
        except Exception as exc:
            last_error = exc
            raise

    if last_status == 429:
        raise ValueError("gemini_quota_exceeded")
    if last_error is not None:
        raise last_error
    return []


def match_scoreboard_rows(
    rows: list[ParsedScoreboardRow],
    registrations: list[Registration],
) -> ScoreboardMatchResult:
    matched: dict[int, PlayerMatchStats] = {}
    unmatched_rows: list[ParsedScoreboardRow] = []
    remaining = list(registrations)

    alias_map: dict[str, Registration] = {}
    display_names: dict[str, Registration] = {}
    for registration in registrations:
        for alias in _registration_aliases(registration):
            alias_map[alias] = registration
        display_names[_normalize_name(registration.game_nick.split("#")[0])] = registration

    for row in rows:
        if not row.raw_name:
            continue
        row_key = _normalize_name(row.raw_name)
        registration = alias_map.get(row_key)
        if not registration:
            close = get_close_matches(row_key, list(alias_map.keys()), n=1, cutoff=0.68)
            if close:
                registration = alias_map[close[0]]
        if not registration:
            close = get_close_matches(
                row_key,
                list(display_names.keys()),
                n=1,
                cutoff=0.68,
            )
            if close:
                registration = display_names[close[0]]
        if registration and registration.id not in matched:
            matched[registration.id] = row.stats
            if registration in remaining:
                remaining.remove(registration)
        else:
            unmatched_rows.append(row)

    return ScoreboardMatchResult(
        matched=matched,
        unmatched_rows=unmatched_rows,
        unmatched_players=remaining,
    )


async def parse_scoreboard_image(
    image_bytes: bytes,
    registrations: list[Registration],
) -> ScoreboardMatchResult:
    matched: dict[int, PlayerMatchStats] = {}
    rows: list[ParsedScoreboardRow] = []
    ocr_text = ""
    gemini_invalid_key = bool(settings.GEMINI_API_KEY and not _is_valid_gemini_api_key())
    gemini_quota_error = False

    if settings.GEMINI_API_KEY and not gemini_invalid_key:
        try:
            rows = await _parse_with_gemini_vision(image_bytes)
            matched = match_scoreboard_rows(rows, registrations).matched
        except ValueError as exc:
            if str(exc) == "invalid_gemini_key":
                gemini_invalid_key = True
            elif str(exc) == "gemini_quota_exceeded":
                gemini_quota_error = True
            else:
                logger.warning(f"Gemini vision parse failed: {exc}")
        except Exception as exc:
            logger.warning(f"Gemini vision parse failed: {type(exc).__name__}")
    elif gemini_invalid_key:
        _validate_gemini_api_key()

    if len(matched) < len(registrations) and settings.OPENAI_API_KEY:
        try:
            rows = await _parse_with_openai_vision(image_bytes)
            logger.info(f"OpenAI vision extracted {len(rows)} scoreboard rows")
            matched = _merge_matched(
                match_scoreboard_rows(rows, registrations).matched,
                matched,
            )
        except Exception as exc:
            logger.warning(f"OpenAI vision parse failed: {exc}")

    if len(matched) < len(registrations):
        try:
            rows, matched_by_nick, ocr_text = _parse_with_tesseract(image_bytes, registrations)
            matched = _merge_matched(matched_by_nick, matched)
            if len(matched) < len(registrations):
                row_result = match_scoreboard_rows(rows, registrations)
                matched = _merge_matched(matched, row_result.matched)
        except Exception as exc:
            logger.warning(f"Tesseract parse failed: {exc}")

    if len(matched) < len(registrations):
        logger.warning(
            "Scoreboard OCR matched only {}/{} players. OCR sample: {}",
            len(matched),
            len(registrations),
            ocr_text[:1200].replace("\n", " | "),
        )
        raise ValueError(
            "Не удалось распознать таблицу на скрине. "
            f"Сопоставлено {len(matched)}/{len(registrations)} игроков. "
            + (
                "Неверный GEMINI_API_KEY. Создайте ключ на https://aistudio.google.com/app/apikey "
                "(формат AQ... или AIza...). "
                if gemini_invalid_key
                else (
                    "Квота Gemini исчерпана или недоступна в вашем регионе (429). "
                    "Проверьте https://ai.dev/rate-limit и настройки проекта в AI Studio. "
                    if gemini_quota_error
                    else "Отправьте скрин как файл (📎 без сжатия) или проверьте GEMINI_API_KEY в .env. "
                )
            )
            + "Можно использовать «✏️ Ввести вручную»."
        )

    remaining = [item for item in registrations if item.id not in matched]
    return ScoreboardMatchResult(
        matched=matched,
        unmatched_rows=[],
        unmatched_players=remaining,
    )


def format_scoreboard_preview(result: ScoreboardMatchResult, registrations: list[Registration]) -> str:
    from bot.utils.stats_format import format_acs, format_kd_ratio

    reg_map = {item.id: item for item in registrations}
    lines = ["📸 Распознанная статистика:\n"]
    for reg_id, stats in result.matched.items():
        registration = reg_map[reg_id]
        lines.append(
            f"• {registration.game_nick}: ACS {format_acs(stats.acs)}, "
            f"K/D {format_kd_ratio(stats.kills, stats.deaths)}, "
            f"K/D/A {stats.kills}/{stats.deaths}/{stats.assists}, "
            f"экон {stats.econ_rating}, FB {stats.first_bloods}, "
            f"spike {stats.spikes_planted}/{stats.spikes_defused}"
        )
    if result.unmatched_rows:
        lines.append("\n⚠️ Не сопоставлены строки со скрина:")
        for row in result.unmatched_rows:
            lines.append(f"  • {row.raw_name}")
    if result.unmatched_players:
        lines.append("\n⚠️ Не найдены в турнире:")
        for registration in result.unmatched_players:
            lines.append(f"  • {registration.game_nick}")
    lines.append("\nСохранить эти результаты?")
    return "\n".join(lines)


def serialize_player_stats(stats_map: dict[int, PlayerMatchStats]) -> dict[str, dict]:
    return {
        str(reg_id): {
            "acs": str(stats.acs),
            "kills": stats.kills,
            "deaths": stats.deaths,
            "assists": stats.assists,
            "econ_rating": stats.econ_rating,
            "first_bloods": stats.first_bloods,
            "spikes_planted": stats.spikes_planted,
            "spikes_defused": stats.spikes_defused,
        }
        for reg_id, stats in stats_map.items()
    }


def deserialize_player_stats(raw: dict[str, dict]) -> dict[int, PlayerMatchStats]:
    return {
        int(reg_id): PlayerMatchStats(
            acs=Decimal(values["acs"]),
            kills=values["kills"],
            deaths=values["deaths"],
            assists=values["assists"],
            econ_rating=values["econ_rating"],
            first_bloods=values["first_bloods"],
            spikes_planted=values["spikes_planted"],
            spikes_defused=values["spikes_defused"],
        )
        for reg_id, values in raw.items()
    }
