from __future__ import annotations

import calendar
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

from .models import Group, Lead, MonthPlan, Operator, Settings

NO_GROUP = "__none__"


def js_round(value: float) -> int:
    return math.floor(value + 0.5)


def parse_day(s: str) -> date:
    return date.fromisoformat(s[:10])


def days_in_month(month: str) -> list[date]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return [date(y, m, d) for d in range(1, last + 1)]


def is_workday(d: date, settings: Settings) -> bool:
    # JS side uses 1=Mon ... 7=Sun. Python isoweekday() matches this.
    return d.isoweekday() in settings.workdays and d.isoformat() not in set(settings.holidays)


def effective_workdays(month: str, settings: Settings) -> list[date]:
    all_days = days_in_month(month)
    work = [d for d in all_days if is_workday(d, settings)]
    return work or all_days


def employment_window(op: Operator, month: str) -> tuple[date, date] | None:
    ds = days_in_month(month)
    first, last = ds[0], ds[-1]
    start = parse_day(op.hire_date) if op.hire_date else first
    end = parse_day(op.fire_date) if op.fire_date else last
    start = max(first, start)
    end = min(last, end)
    if start > end:
        return None
    return start, end


def plan_id(month: str, scope: str, target_id: str | None) -> str:
    if scope == "team":
        return f"{month}|team"
    return f"{month}|{scope}|{target_id or ''}"


class MetricsCache:
    def __init__(self) -> None:
        self.leads: dict[str, Lead] = {}
        self.op_day: dict[tuple[str, str], int] = defaultdict(int)
        self.group_day: dict[tuple[str, str], int] = defaultdict(int)
        self.team_day: dict[str, int] = defaultdict(int)

    def load(self, leads: Iterable[Lead]) -> None:
        self.leads.clear()
        self.op_day.clear()
        self.group_day.clear()
        self.team_day.clear()
        for lead in leads:
            self.leads[lead.id] = lead
            if lead.counts:
                self._add(lead, +1)

    def _add(self, lead: Lead, delta: int) -> None:
        day = lead.day
        self.op_day[(lead.operator_id, day)] += delta
        if self.op_day[(lead.operator_id, day)] <= 0:
            self.op_day.pop((lead.operator_id, day), None)
        g = lead.group_id or NO_GROUP
        self.group_day[(g, day)] += delta
        if self.group_day[(g, day)] <= 0:
            self.group_day.pop((g, day), None)
        self.team_day[day] += delta
        if self.team_day[day] <= 0:
            self.team_day.pop(day, None)

    def apply(self, lead: Lead) -> bool:
        """Apply current DB row. Returns True when counted metrics changed."""
        old = self.leads.get(lead.id)
        if old and old.counts:
            self._add(old, -1)
        self.leads[lead.id] = lead
        if lead.counts:
            self._add(lead, +1)
        if not old:
            return lead.counts
        return (old.counts, old.day, old.operator_id, old.group_id) != (lead.counts, lead.day, lead.operator_id, lead.group_id)

    def op_day_count(self, op_id: str, day: str) -> int:
        return self.op_day.get((op_id, day), 0)

    def group_day_count(self, group_id: str | None, day: str) -> int:
        return self.group_day.get((group_id or NO_GROUP, day), 0)

    def team_day_count(self, day: str) -> int:
        return self.team_day.get(day, 0)

    def op_month_count(self, op_id: str, month: str) -> int:
        return sum(n for (oid, d), n in self.op_day.items() if oid == op_id and d.startswith(month))

    def group_month_count(self, group_id: str | None, month: str) -> int:
        key = group_id or NO_GROUP
        return sum(n for (gid, d), n in self.group_day.items() if gid == key and d.startswith(month))

    def team_month_count(self, month: str) -> int:
        return sum(n for d, n in self.team_day.items() if d.startswith(month))

    def op_previous_record(self, op_id: str, before_day: str) -> int:
        vals = [n for (oid, d), n in self.op_day.items() if oid == op_id and d < before_day]
        return max(vals, default=0)

    def group_previous_record(self, group_id: str | None, before_day: str) -> int:
        key = group_id or NO_GROUP
        vals = [n for (gid, d), n in self.group_day.items() if gid == key and d < before_day]
        return max(vals, default=0)

    def team_previous_record(self, before_day: str) -> int:
        vals = [n for d, n in self.team_day.items() if d < before_day]
        return max(vals, default=0)


@dataclass(slots=True)
class PlanResolver:
    operators: dict[str, Operator]
    groups: dict[str, Group]
    plans: dict[str, MonthPlan]
    settings: Settings

    def operator_plan(self, op: Operator, month: str) -> float:
        rec = self.plans.get(plan_id(month, "operator", op.id))
        if rec:
            return rec.plan
        base = op.monthly_plan if op.monthly_plan is not None else self.settings.default_operator_plan
        if base <= 0:
            return 0
        win = employment_window(op, month)
        if not win:
            return 0
        work = effective_workdays(month, self.settings)
        total = max(1, len(work))
        in_window = sum(1 for d in work if win[0] <= d <= win[1])
        share = min(1.0, in_window / total)
        return float(js_round(base * share))

    def operator_daily_plan(self, op: Operator, day: str) -> float:
        month = day[:7]
        plan = self.operator_plan(op, month)
        if plan <= 0:
            return 0
        win = employment_window(op, month)
        if not win:
            return 0
        d = parse_day(day)
        work = effective_workdays(month, self.settings)
        work_set = set(work)
        if d not in work_set or d < win[0] or d > win[1]:
            return 0
        wd = sum(1 for x in work if win[0] <= x <= win[1])
        return plan / wd if wd else 0

    def group_plan(self, group_id: str | None, month: str) -> float:
        if group_id is None:
            return sum(self.operator_plan(o, month) for o in self.operators.values() if o.group_id is None and not o.deleted_at)
        rec = self.plans.get(plan_id(month, "group", group_id))
        if rec:
            return rec.plan
        g = self.groups.get(group_id)
        if g and g.monthly_plan > 0:
            return g.monthly_plan
        return sum(self.operator_plan(o, month) for o in self.operators.values() if o.group_id == group_id and not o.deleted_at)

    def team_plan(self, month: str) -> float:
        rec = self.plans.get(plan_id(month, "team", None))
        if rec:
            return rec.plan
        if self.settings.team_plan > 0:
            return self.settings.team_plan
        group_ids = {g.id for g in self.groups.values() if not g.deleted_at}
        if any(o.group_id is None and not o.deleted_at for o in self.operators.values()):
            group_ids.add(NO_GROUP)
        total = 0.0
        for gid in group_ids:
            total += self.group_plan(None if gid == NO_GROUP else gid, month)
        return total
