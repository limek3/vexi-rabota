from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(slots=True)
class Lead:
    id: str
    at: str
    operator_id: str
    group_id: str | None
    status: str
    updated_at: str

    @property
    def day(self) -> str:
        return self.at[:10]

    @property
    def month(self) -> str:
        return self.at[:7]

    @property
    def counts(self) -> bool:
        # Telegram achievements are based only on leads approved by a supervisor.
        # LEADUP stores the visible status «Доведён» as ``done``.
        # ``work`` (В работе) and ``failed`` (Не доведён) must not affect
        # grades, records, daily plans or monthly plans.
        return self.status == "done"


@dataclass(slots=True)
class Operator:
    id: str
    name: str
    group_id: str | None
    status: str
    hire_date: str
    fire_date: str
    monthly_plan: float | None
    deleted_at: str | None


@dataclass(slots=True)
class Group:
    id: str
    name: str
    monthly_plan: float
    active: bool
    deleted_at: str | None


@dataclass(slots=True)
class MonthPlan:
    id: str
    month: str
    scope: str
    target_id: str | None
    plan: float


@dataclass(slots=True)
class Settings:
    team_plan: float = 0
    default_operator_plan: float = 0
    workdays: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 5])
    holidays: list[str] = field(default_factory=list)
    company_name: str = "Отдел лидогенерации"

    @classmethod
    def from_json(cls, raw: dict[str, Any] | None) -> "Settings":
        raw = raw or {}
        workdays = raw.get("workdays")
        if not isinstance(workdays, list) or not workdays:
            workdays = [1, 2, 3, 4, 5]
        holidays = raw.get("holidays")
        if not isinstance(holidays, list):
            holidays = []
        return cls(
            team_plan=float(raw.get("teamPlan") or 0),
            default_operator_plan=float(raw.get("defaultOperatorPlan") or 0),
            workdays=[int(x) for x in workdays if isinstance(x, (int, float, str)) and str(x).isdigit()],
            holidays=[str(x) for x in holidays],
            company_name=str(raw.get("companyName") or "Отдел лидогенерации"),
        )


def month_start(month: str) -> date:
    year, mon = map(int, month.split("-"))
    return date(year, mon, 1)


@dataclass(slots=True)
class TelegramLink:
    """Привязка карточки оператора к Telegram (таблица operator_telegram).

    Идентификатор человека — только ``telegram_user_id``. username/имя — для показа.
    ``tag_synced`` — последний тег, который Vexi подтвердил в рабочем чате
    ('' — тег снят, None — неизвестно, надо проверить).
    """

    operator_id: str
    telegram_user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    tag_synced: str | None = None
    tag_chat_id: int | None = None
    chat_status: str = "unknown"
    last_error: str | None = None

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> TelegramLink:
        return cls(
            operator_id=str(r["operator_id"]),
            telegram_user_id=int(r["telegram_user_id"]),
            username=r.get("telegram_username"),
            first_name=r.get("telegram_first_name"),
            last_name=r.get("telegram_last_name"),
            tag_synced=r.get("tag_synced"),
            tag_chat_id=(int(r["tag_chat_id"]) if r.get("tag_chat_id") is not None else None),
            chat_status=str(r.get("chat_status") or "unknown"),
            last_error=r.get("last_error"),
        )


@dataclass(slots=True)
class PendingUnlink:
    """Отвязанный Telegram, с которого бот ещё не снял тег (таблица telegram_unlinks)."""

    id: int
    operator_id: str
    telegram_user_id: int
    unlinked_at: str
