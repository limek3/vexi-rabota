"""Итоги дня: одно сообщение в рабочий чат в заданное время (по умолчанию 20:00 по APP_TIMEZONE).

Считается как в LEADUP, но лиды — только «доведён» (как и поздравления бота): «в работе» —
промежуточный статус, такие лиды показываются отдельно — «на проверке»;
  - часы — смены «работа» и «обучение»;
  - план дня отдела — план месяца ÷ рабочие дни месяца; в нерабочий день плана нет;
  - план на дату — план месяца × (рабочие дни по сегодня ÷ все рабочие дни).

Здесь только расчёт и текст (без сети) — чтобы их можно было проверить тестами.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .messages import e
from .metrics import PlanResolver, effective_workdays, is_workday
from .models import Group, MonthPlan, Operator, Settings

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
MEDALS = ["🥇", "🥈", "🥉"]
HOUR_TYPES = ("work", "training")


@dataclass(slots=True)
class DayLead:
    at: str
    operator_id: str
    group_id: str | None
    status: str

    @property
    def day(self) -> str:
        return self.at[:10]

    @property
    def counts(self) -> bool:
        # в итоги идут только доведённые; «в работе» — ещё на проверке у супервайзера
        return self.status == "done"


@dataclass(slots=True)
class DayShift:
    date: str
    operator_id: str
    hours: float
    type: str


@dataclass(slots=True)
class OpLine:
    op: Operator
    leads: int
    hours: float


@dataclass(slots=True)
class Summary:
    day: str
    company: str
    leads: int
    # «в работе» за день — ещё на проверке, в итог не входят
    pending: int
    hours: float
    day_plan: float
    conv_norm: float
    top: list[OpLine]
    no_leads: list[OpLine]
    groups: list[tuple[str, int, float]]
    week_fact: int
    week_plan: float
    month_fact: int
    month_plan: float
    plan_to_date: float
    workdays_left: int
    tomorrow: list[OpLine] = field(default_factory=list)
    tomorrow_day: str = ""


def short_name(name: str) -> str:
    """«Фамилия И. О.» — как в CRM."""
    parts = (name or "").split()
    if len(parts) < 2:
        return name
    return parts[0] + " " + " ".join(p[0] + "." for p in parts[1:])


def num(v: float) -> str:
    """Число как в CRM: целое без дробной части, иначе одна цифра после запятой."""
    r = round(v, 1)
    if r == int(r):
        return f"{int(r):,}".replace(",", " ")
    return f"{r:,.1f}".replace(",", " ").replace(".", ",")


def pct(v: float) -> str:
    return f"{math.floor(v * 100 + 0.5)}%"


def bar(ratio: float, cells: int = 10) -> str:
    """Полоска прогресса ▰▱; больше 100% — полная."""
    filled = max(0, min(cells, math.floor(ratio * cells + 0.5)))
    return "▰" * filled + "▱" * (cells - filled)


def day_title(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]}"


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    x = abs(n) % 100
    y = x % 10
    if 11 <= x <= 14:
        return forms[2]
    if y == 1:
        return forms[0]
    if 2 <= y <= 4:
        return forms[1]
    return forms[2]


def _team_day_plan(p: PlanResolver, settings: Settings, d: date) -> float:
    if not is_workday(d, settings):
        return 0.0
    month = d.isoformat()[:7]
    return p.team_plan(month) / max(1, len(effective_workdays(month, settings)))


def build_summary(
    day: str,
    leads: list[DayLead],
    shifts: list[DayShift],
    operators: dict[str, Operator],
    groups: dict[str, Group],
    plans: dict[str, MonthPlan],
    settings: Settings,
) -> Summary:
    """leads — с начала недели или месяца (что раньше) по день включительно; shifts — там же + завтра."""
    p = PlanResolver(operators, groups, plans, settings)
    d = date.fromisoformat(day)
    month = day[:7]
    tomorrow = (d + timedelta(days=1)).isoformat()

    by_op_day: dict[tuple[str, str], int] = defaultdict(int)
    by_group_day: dict[tuple[str | None, str], int] = defaultdict(int)
    by_day: dict[str, int] = defaultdict(int)
    pending = sum(1 for lead in leads if lead.day == day and lead.status == "work")
    for lead in leads:
        if not lead.counts or lead.day > day:
            continue
        by_op_day[(lead.operator_id, lead.day)] += 1
        by_group_day[(lead.group_id, lead.day)] += 1
        by_day[lead.day] += 1

    hours: dict[tuple[str, str], float] = defaultdict(float)
    for s in shifts:
        if s.type in HOUR_TYPES and s.hours > 0:
            hours[(s.operator_id, s.date)] += s.hours

    # люди дня: кто был на смене или передал лиды
    worked = {op_id for (op_id, dd) in hours if dd == day} | {op_id for (op_id, dd) in by_op_day if dd == day}
    lines = [OpLine(operators[op_id], by_op_day.get((op_id, day), 0), hours.get((op_id, day), 0.0)) for op_id in worked if op_id in operators]
    with_leads = sorted((x for x in lines if x.leads > 0), key=lambda x: (-x.leads, x.hours, x.op.name))
    no_leads = sorted((x for x in lines if x.leads == 0 and x.hours > 0), key=lambda x: x.op.name)

    # группы: только если их несколько — иначе строка повторяет итог отдела
    live_groups = [g for g in groups.values() if not g.deleted_at]
    group_lines: list[tuple[str, int, float]] = []
    if len(live_groups) > 1:
        work = effective_workdays(month, settings)
        for g in sorted(live_groups, key=lambda x: x.name):
            fact = by_group_day.get((g.id, day), 0)
            gp = p.group_plan(g.id, month) / max(1, len(work)) if is_workday(d, settings) else 0.0
            if fact or gp:
                group_lines.append((g.name, fact, gp))

    # неделя пн–вс (по день включительно); план — по дням недели, каждый день из своего месяца
    week_start = d - timedelta(days=d.weekday())
    week_days = [week_start + timedelta(days=i) for i in range(7)]
    week_fact = sum(by_day.get(x.isoformat(), 0) for x in week_days if x <= d)
    week_plan = sum(_team_day_plan(p, settings, x) for x in week_days)

    month_plan = p.team_plan(month)
    work_days = effective_workdays(month, settings)
    passed = sum(1 for x in work_days if x <= d)
    plan_to_date = month_plan * passed / max(1, len(work_days))
    month_fact = sum(n for dd, n in by_day.items() if dd.startswith(month))

    tomorrow_lines = sorted(
        (OpLine(operators[op_id], 0, h) for (op_id, dd), h in hours.items() if dd == tomorrow and op_id in operators and not operators[op_id].deleted_at),
        key=lambda x: x.op.name,
    )

    return Summary(
        day=day,
        # подпись под заголовком: одна группа — её название, иначе — название отдела
        company=live_groups[0].name if len(live_groups) == 1 else settings.company_name,
        leads=by_day.get(day, 0),
        pending=pending,
        hours=sum(h for (_, dd), h in hours.items() if dd == day),
        day_plan=_team_day_plan(p, settings, d),
        conv_norm=settings.conv_norm_pct / 100,
        top=with_leads[:3],
        no_leads=no_leads,
        groups=group_lines,
        week_fact=week_fact,
        week_plan=week_plan,
        month_fact=month_fact,
        month_plan=month_plan,
        plan_to_date=plan_to_date,
        workdays_left=len(work_days) - passed,
        tomorrow=tomorrow_lines,
        tomorrow_day=tomorrow,
    )


def is_quiet(s: Summary) -> bool:
    """Никто не работал и лидов нет — выходной, сообщение не нужно."""
    return s.leads == 0 and s.pending == 0 and s.hours == 0


def render(s: Summary) -> str:
    """HTML для Telegram — формат, согласованный с руководителем (макет «итоги дня»):
    заголовок, день (лиды к плану, часы и конверсия), лучшие, без лидов, неделя и месяц, завтра."""
    d = date.fromisoformat(s.day)
    out: list[str] = [f"📊 <b>Итоги дня · {e(day_title(s.day))}</b>", e(s.company), ""]

    # день
    plan = math.ceil(s.day_plan - 1e-9) if s.day_plan > 0 else 0
    if plan:
        ratio = s.leads / s.day_plan
        mark = "✅" if s.leads >= plan else "⚠️"
        out.append(f"Лиды: <b>{s.leads}</b> при плане {plan} — <b>{pct(ratio)}</b> {mark}")
    else:
        out.append(f"Лиды: <b>{s.leads}</b>")
    if s.hours > 0:
        out.append(f"Часы: {num(s.hours)} · конверсия <b>{pct(s.leads / s.hours)}</b>")
    if s.pending:
        out.append(f"⏳ Ещё на проверке: {s.pending} {_plural(s.pending, ('лид', 'лида', 'лидов'))}")
    for name, fact, gp in s.groups:
        g_plan = math.ceil(gp - 1e-9) if gp > 0 else 0
        out.append(f"👥 {e(name)}: <b>{fact}</b>" + (f" при плане {g_plan}" if g_plan else ""))

    # лучшие дня
    if s.top:
        out.append("")
        best = s.top[0]
        out.append(f"🏆 Лучший оператор: <b>{e(short_name(best.op.name))}</b> — {_leads_for(best)}")
        if len(s.top) > 1:
            second = s.top[1]
            out.append(f"👏 {e(short_name(second.op.name))} — {_leads_for(second)}")

    # без лидов
    if s.no_leads:
        out.append("")
        who = ", ".join(f"{e(short_name(x.op.name))} — {num(x.hours)} ч на смене" for x in s.no_leads)
        out.append(f"⚠️ <b>Без лидов:</b> {who}")

    # неделя и месяц
    out.append("")
    week_start = d - timedelta(days=d.weekday())
    week_end = week_start + timedelta(days=6)
    span = f"{week_start.day}–{week_end.day:02d}.{week_end.month:02d}" if week_start.month == week_end.month else f"{week_start.day:02d}.{week_start.month:02d}–{week_end.day:02d}.{week_end.month:02d}"
    if s.week_plan > 0:
        out.append(f"📅 <b>Неделя</b> ({span}): {s.week_fact} из {math.ceil(s.week_plan - 1e-9)} по плану")
    else:
        out.append(f"📅 <b>Неделя</b> ({span}): {s.week_fact}")
    if s.month_plan > 0:
        mp = math.ceil(s.month_plan - 1e-9)
        gap = math.floor(s.month_fact - s.plan_to_date + 0.5)
        left = mp - s.month_fact
        if left <= 0:
            out.append(f"📈 <b>Месяц:</b> {s.month_fact} из {mp} · план месяца выполнен 🎉")
        else:
            tail = f"до плана на дату не хватает <b>{-gap}</b>" if gap < 0 else f"впереди плана на дату на <b>{gap}</b>"
            out.append(f"📈 <b>Месяц:</b> {s.month_fact} из {mp} · {tail}")
            if s.workdays_left > 0:
                days = _plural(s.workdays_left, ("рабочий день", "рабочих дня", "рабочих дней"))
                out.append(f"Осталось {left} — это ≈ <b>{num(left / s.workdays_left)}</b> в день ({s.workdays_left} {days})")
            else:
                out.append(f"Осталось {left} — рабочих дней в месяце больше нет")
    else:
        out.append(f"📈 <b>Месяц:</b> {s.month_fact}")

    # завтра
    out.append("")
    if s.tomorrow:
        people = ", ".join(f"{e(short_name(x.op.name))} ({num(x.hours)} ч)" for x in s.tomorrow)
        out.append(f"Завтра на смене: {people}")
    else:
        out.append("Завтра смен в графике нет")
    return "\n".join(out)


def _leads_for(x: OpLine) -> str:
    """«8 лидов за 7,5 ч»"""
    h = f" за {num(x.hours)} ч" if x.hours > 0 else ""
    return f"{x.leads} {_plural(x.leads, ('лид', 'лида', 'лидов'))}{h}"
