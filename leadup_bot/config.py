from __future__ import annotations

import os
from dataclasses import dataclass
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

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            telegram_token=_required("TELEGRAM_BOT_TOKEN"),
            supabase_url=_required("SUPABASE_URL").rstrip("/"),
            supabase_service_key=_required("SUPABASE_SERVICE_ROLE_KEY"),
            setup_secret=_required("BOT_SETUP_SECRET"),
            admin_ids=_ids("TELEGRAM_ADMIN_IDS"),
            poll_interval=_int("POLL_INTERVAL_SECONDS", 4),
            reference_refresh=_int("REFERENCE_REFRESH_SECONDS", 30),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO",
        )
