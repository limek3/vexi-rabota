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
