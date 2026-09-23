from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Awaitable, Callable

from . import messages
from .db import SupabaseDB
from .metrics import MetricsCache, PlanResolver
from .models import Group, Lead, MonthPlan, Operator, Settings

log = logging.getLogger(__name__)
Send = Callable[[str], Awaitable[None]]
# Вызывается при любом изменении лида для затронутых операторов — до поздравлений,
# чтобы тег в чате сменился раньше, чем придёт сообщение о новом грейде.
OperatorHook = Callable[[str], Awaitable[None]]
EVENT_NAMESPACE = "done_v2"


@dataclass(slots=True)
class ReferenceData:
    operators: dict[str, Operator]
    groups: dict[str, Group]
    plans: dict[str, MonthPlan]
    settings: Settings


class EventEngine:
    def __init__(
        self, db: SupabaseDB, cache: MetricsCache, ref: ReferenceData, sender: Send,
        on_operator_change: OperatorHook | None = None,
    ) -> None:
        self.db = db
        self.cache = cache
        self.ref = ref
        self.sender = sender
        self.on_operator_change = on_operator_change

    @property
    def plans(self) -> PlanResolver:
        return PlanResolver(self.ref.operators, self.ref.groups, self.ref.plans, self.ref.settings)

    def update_reference(self, ref: ReferenceData) -> None:
        self.ref = ref

    @staticmethod
    def _event_key(key: str) -> str:
        # v2 separates approved-only achievements from the old implementation
        # where both ``work`` and ``done`` leads were counted. Keeping a new
        # namespace means existing rows in telegram_bot_events cannot suppress
        # a future correct notification.
        return f"{EVENT_NAMESPACE}:{key}"

    async def _emit(self, key: str, typ: str, text: str, payload: dict) -> None:
        event_key = self._event_key(key)
        claimed = await self.db.claim_event(event_key, typ, payload)
        if not claimed:
            return
        try:
            await self.sender(text)
            log.info("Sent event %s", event_key)
        except Exception:
            await self.db.release_event(event_key)
            raise

    async def _notify_operators(self, lead: Lead, old: Lead | None) -> None:
        """Грейд может и расти (work → done), и падать (done → failed, лид переназначен) —
        сообщаем о каждом затронутом операторе. Ошибка тегов не мешает поздравлениям."""
        if not self.on_operator_change:
            return
        affected = [lead.operator_id]
        if old and old.operator_id != lead.operator_id:
            affected.append(old.operator_id)
        for op_id in affected:
            try:
                await self.on_operator_change(op_id)
            except Exception:
                log.exception("Operator change hook failed for %s", op_id)

    async def process_lead_change(self, lead: Lead) -> None:
        # Re-evaluate even when this exact row is retried after a transient error.
        # Event keys make the operation idempotent, while this prevents a failed Telegram
        # send from being lost merely because the in-memory cache already saw the row.
        old = self.cache.leads.get(lead.id)
        self.cache.apply(lead)
        await self._notify_operators(lead, old)
        if not lead.counts:
            return
        op = self.ref.operators.get(lead.operator_id)
        if not op:
            log.warning("Unknown operator %s for lead %s", lead.operator_id, lead.id)
            return
        day = lead.day
        month = lead.month
        count = self.cache.op_day_count(op.id, day)

        # 1) Shift grade thresholds. Exact thresholds prevent false events if a bulk import jumps over several counts.
        if count in (6, 8, 11):
            grade_no = {6: "II", 8: "III", 11: "IV"}[count]
            await self._emit(
                f"grade:{day}:{op.id}:{count}", "grade", messages.grade(op.name, count),
                {"operator_id": op.id, "operator_name": op.name, "day": day, "count": count, "grade": grade_no},
            )

        # 2) Personal record: public only from 6+, and only first record break per operator per day.
        previous = self.cache.op_previous_record(op.id, day)
        if count >= 6 and previous > 0 and count > previous:
            await self._emit(
                f"personal_record:{day}:{op.id}", "personal_record", messages.personal_record(op.name, count, previous),
                {"operator_id": op.id, "operator_name": op.name, "day": day, "count": count, "previous": previous},
            )

        # 3) Daily personal plan. Same logic as LEADUP: month plan / workdays inside employment window.
        dp = self.plans.operator_daily_plan(op, day)
        target = math.ceil(dp - 1e-9) if dp > 0 else 0
        if target > 0 and count >= target:
            await self._emit(
                f"daily_plan:{day}:{op.id}", "daily_plan", messages.daily_plan(op.name, count, dp),
                {"operator_id": op.id, "operator_name": op.name, "day": day, "fact": count, "plan": dp},
            )

        # 4) Personal monthly plan / early 100%.
        op_plan = self.plans.operator_plan(op, month)
        op_month = self.cache.op_month_count(op.id, month)
        if op_plan > 0 and op_month >= math.ceil(op_plan - 1e-9):
            await self._emit(
                f"month_plan:operator:{month}:{op.id}", "month_plan_operator", messages.operator_month_plan(op.name, op_month, op_plan, day),
                {"operator_id": op.id, "operator_name": op.name, "month": month, "fact": op_month, "plan": op_plan, "day": day},
            )

        # 5) Group monthly plan.
        if lead.group_id:
            group = self.ref.groups.get(lead.group_id)
            gp = self.plans.group_plan(lead.group_id, month)
            gm = self.cache.group_month_count(lead.group_id, month)
            if group and gp > 0 and gm >= math.ceil(gp - 1e-9):
                await self._emit(
                    f"month_plan:group:{month}:{group.id}", "month_plan_group", messages.group_month_plan(group.name, gm, gp, day),
                    {"group_id": group.id, "group_name": group.name, "month": month, "fact": gm, "plan": gp, "day": day},
                )

            # 6) Group day record. Need an existing historical record, otherwise a group's first working day isn't announced as a record.
            current_group = self.cache.group_day_count(lead.group_id, day)
            previous_group = self.cache.group_previous_record(lead.group_id, day)
            if group and previous_group > 0 and current_group > previous_group:
                await self._emit(
                    f"group_record:{day}:{group.id}", "group_record", messages.group_record(group.name, current_group, previous_group),
                    {"group_id": group.id, "group_name": group.name, "day": day, "count": current_group, "previous": previous_group},
                )

        # 7) Team monthly plan.
        tp = self.plans.team_plan(month)
        tm = self.cache.team_month_count(month)
        if tp > 0 and tm >= math.ceil(tp - 1e-9):
            await self._emit(
                f"month_plan:team:{month}", "month_plan_team", messages.team_month_plan(self.ref.settings.company_name, tm, tp, day),
                {"month": month, "fact": tm, "plan": tp, "day": day},
            )

        # 8) Team day record.
        current_team = self.cache.team_day_count(day)
        previous_team = self.cache.team_previous_record(day)
        if previous_team > 0 and current_team > previous_team:
            await self._emit(
                f"team_record:{day}", "team_record", messages.team_record(self.ref.settings.company_name, current_team, previous_team),
                {"day": day, "count": current_team, "previous": previous_team},
            )

    async def reconcile_day(self, day: str) -> None:
        """Recreate any achievement that happened while the bot process was offline."""
        month = day[:7]
        p = self.plans

        for op in self.ref.operators.values():
            count = self.cache.op_day_count(op.id, day)
            if count <= 0:
                continue
            for threshold in (6, 8, 11):
                if count >= threshold:
                    grade_no = {6: "II", 8: "III", 11: "IV"}[threshold]
                    await self._emit(
                        f"grade:{day}:{op.id}:{threshold}", "grade", messages.grade(op.name, threshold),
                        {"operator_id": op.id, "operator_name": op.name, "day": day, "count": threshold, "grade": grade_no, "reconciled": True},
                    )
            previous = self.cache.op_previous_record(op.id, day)
            if count >= 6 and previous > 0 and count > previous:
                await self._emit(
                    f"personal_record:{day}:{op.id}", "personal_record", messages.personal_record(op.name, count, previous),
                    {"operator_id": op.id, "operator_name": op.name, "day": day, "count": count, "previous": previous, "reconciled": True},
                )
            dp = p.operator_daily_plan(op, day)
            if dp > 0 and count >= math.ceil(dp - 1e-9):
                await self._emit(
                    f"daily_plan:{day}:{op.id}", "daily_plan", messages.daily_plan(op.name, count, dp),
                    {"operator_id": op.id, "operator_name": op.name, "day": day, "fact": count, "plan": dp, "reconciled": True},
                )
            op_plan = p.operator_plan(op, month)
            op_month = self.cache.op_month_count(op.id, month)
            if op_plan > 0 and op_month >= math.ceil(op_plan - 1e-9):
                await self._emit(
                    f"month_plan:operator:{month}:{op.id}", "month_plan_operator", messages.operator_month_plan(op.name, op_month, op_plan, day),
                    {"operator_id": op.id, "operator_name": op.name, "month": month, "fact": op_month, "plan": op_plan, "day": day, "reconciled": True},
                )

        for group in self.ref.groups.values():
            if group.deleted_at:
                continue
            gp = p.group_plan(group.id, month)
            gm = self.cache.group_month_count(group.id, month)
            if gp > 0 and gm >= math.ceil(gp - 1e-9):
                await self._emit(
                    f"month_plan:group:{month}:{group.id}", "month_plan_group", messages.group_month_plan(group.name, gm, gp, day),
                    {"group_id": group.id, "group_name": group.name, "month": month, "fact": gm, "plan": gp, "day": day, "reconciled": True},
                )
            current_group = self.cache.group_day_count(group.id, day)
            previous_group = self.cache.group_previous_record(group.id, day)
            if previous_group > 0 and current_group > previous_group:
                await self._emit(
                    f"group_record:{day}:{group.id}", "group_record", messages.group_record(group.name, current_group, previous_group),
                    {"group_id": group.id, "group_name": group.name, "day": day, "count": current_group, "previous": previous_group, "reconciled": True},
                )

        tp = p.team_plan(month)
        tm = self.cache.team_month_count(month)
        if tp > 0 and tm >= math.ceil(tp - 1e-9):
            await self._emit(
                f"month_plan:team:{month}", "month_plan_team", messages.team_month_plan(self.ref.settings.company_name, tm, tp, day),
                {"month": month, "fact": tm, "plan": tp, "day": day, "reconciled": True},
            )
        current_team = self.cache.team_day_count(day)
        previous_team = self.cache.team_previous_record(day)
        if previous_team > 0 and current_team > previous_team:
            await self._emit(
                f"team_record:{day}", "team_record", messages.team_record(self.ref.settings.company_name, current_team, previous_team),
                {"day": day, "count": current_team, "previous": previous_team, "reconciled": True},
            )

    def existing_event_keys(self) -> list[tuple[str, str, dict]]:
        """Seed current achievements on the first ever start so the bot doesn't dump historical messages."""
        out: list[tuple[str, str, dict]] = []
        p = self.plans
        for (op_id, day), count in list(self.cache.op_day.items()):
            op = self.ref.operators.get(op_id)
            if not op:
                continue
            for threshold in (6, 8, 11):
                if count >= threshold:
                    out.append((f"grade:{day}:{op_id}:{threshold}", "grade", {"seeded": True}))
            prev = self.cache.op_previous_record(op_id, day)
            if count >= 6 and prev > 0 and count > prev:
                out.append((f"personal_record:{day}:{op_id}", "personal_record", {"seeded": True}))
            dp = p.operator_daily_plan(op, day)
            if dp > 0 and count >= math.ceil(dp - 1e-9):
                out.append((f"daily_plan:{day}:{op_id}", "daily_plan", {"seeded": True}))

        months = {d[:7] for d in self.cache.team_day.keys()}
        for month in months:
            for op in self.ref.operators.values():
                plan = p.operator_plan(op, month)
                if plan > 0 and self.cache.op_month_count(op.id, month) >= math.ceil(plan - 1e-9):
                    out.append((f"month_plan:operator:{month}:{op.id}", "month_plan_operator", {"seeded": True}))
            for group in self.ref.groups.values():
                plan = p.group_plan(group.id, month)
                if plan > 0 and self.cache.group_month_count(group.id, month) >= math.ceil(plan - 1e-9):
                    out.append((f"month_plan:group:{month}:{group.id}", "month_plan_group", {"seeded": True}))
            tp = p.team_plan(month)
            if tp > 0 and self.cache.team_month_count(month) >= math.ceil(tp - 1e-9):
                out.append((f"month_plan:team:{month}", "month_plan_team", {"seeded": True}))

        for (group_id, day), count in list(self.cache.group_day.items()):
            if group_id == "__none__":
                continue
            prev = self.cache.group_previous_record(group_id, day)
            if prev > 0 and count > prev:
                out.append((f"group_record:{day}:{group_id}", "group_record", {"seeded": True}))
        for day, count in list(self.cache.team_day.items()):
            prev = self.cache.team_previous_record(day)
            if prev > 0 and count > prev:
                out.append((f"team_record:{day}", "team_record", {"seeded": True}))
        return [(self._event_key(key), typ, payload) for key, typ, payload in out]
