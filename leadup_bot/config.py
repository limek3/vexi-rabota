from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def _ids(name: str) -> frozenset[int]:
    out: set[int] = set()
    for part in os.getenv(name, "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError as exc:
            raise RuntimeError(f"Environment variable {name} contains invalid Telegram ID: {part}") from exc
    return frozenset(out)


def _tz(name: str, default: str) -> ZoneInfo:
    raw = os.getenv(name, "").strip() or default
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeError(f"Environment variable {name} must be an IANA timezone like Europe/Moscow, got: {raw}") from exc


@dataclass(frozen=True, slots=True)
class Config:
    telegram_token: str
    supabase_url: str
    supabase_service_key: str
    setup_secret: str
    admin_ids: frozenset[int]
    poll_interval: int
    reference_refresh: int
    log_level: str
    # Часовой пояс, в котором считается «сегодня» для дневных грейдов.
    app_timezone: ZoneInfo
    # Имя бота для ссылок t.me/<bot>?start=… Пусто — берётся из getMe() при старте.
    bot_username: str
    # Как часто сверять теги всех привязанных операторов (без лишних запросов к Telegram).
    tag_reconcile_interval: int
    # Как часто перепроверять теги прямо в Telegram (getChatMember), даже если в базе всё сходится.
    tag_verify_interval: int

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            telegram_token=_required("TELEGRAM_BOT_TOKEN"),
            supabase_url=_required("SUPABASE_URL").rstrip("/"),
            supabase_service_key=_required("SUPABASE_SERVICE_ROLE_KEY"),
            setup_secret=_required("BOT_SETUP_SECRET"),
            admin_ids=_ids("TELEGRAM_ADMIN_IDS"),
            poll_interval=_int("POLL_INTERVAL_SECONDS", 4),
            reference_refresh=_int("REFERENCE_REFRESH_SECONDS", 30),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO",
            app_timezone=_tz("APP_TIMEZONE", "Europe/Moscow"),
            bot_username=os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@"),
            tag_reconcile_interval=_int("TAG_RECONCILE_SECONDS", 300, minimum=30),
            tag_verify_interval=_int("TAG_VERIFY_SECONDS", 3600, minimum=300),
        )
