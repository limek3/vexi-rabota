from __future__ import annotations

from datetime import datetime, tzinfo

# Дневной грейд по числу ДОВЕДЁННЫХ (status = done) лидов за текущий календарный день.
# Пороги те же, что у поздравлений в events.py: 6 → II, 8 → III, 11 → IV.
GRADE_THRESHOLDS: tuple[tuple[int, int], ...] = ((11, 4), (8, 3), (6, 2))

# Пользовательский тег участника в рабочем чате. Telegram: до 16 символов, без эмодзи.
GRADE_TAGS: dict[int, str] = {1: "Грейд I", 2: "Грейд II", 3: "Грейд III", 4: "Грейд IV"}
TAG_SLUGS: dict[str, str] = {tag: f"grade{n}" for n, tag in GRADE_TAGS.items()}


def grade_for(done_today: int) -> int:
    for threshold, grade in GRADE_THRESHOLDS:
        if done_today >= threshold:
            return grade
    return 1


def grade_tag(done_today: int) -> str:
    return GRADE_TAGS[grade_for(done_today)]


def tag_slug(tag: str | None) -> str:
    """Короткое имя тега для логов: grade1…grade4, none (снят), unknown."""
    if tag is None:
        return "unknown"
    if tag == "":
        return "none"
    return TAG_SLUGS.get(tag, "custom")


def local_today(tz: tzinfo, now: datetime | None = None) -> str:
    """«Сегодня» в часовом поясе проекта (APP_TIMEZONE), а не по UTC."""
    return (now or datetime.now(tz)).astimezone(tz).date().isoformat()
