from __future__ import annotations

import html
import math
from datetime import date


def e(value: object) -> str:
    return html.escape(str(value), quote=False)


def n(value: float | int) -> str:
    v = float(value)
    return f"{int(v):,}".replace(",", " ") if v.is_integer() else f"{v:,.1f}".replace(",", " ").replace(".0", "")


def plural_leads(value: int) -> str:
    x = abs(value) % 100
    y = x % 10
    if 11 <= x <= 14:
        return "лидов"
    if y == 1:
        return "лид"
    if 2 <= y <= 4:
        return "лида"
    return "лидов"


def grade(name: str, count: int) -> str:
    # Грейд относится только к текущей смене/календарному дню.
    # На следующий день счетчик начинается заново, поэтому оператор снова
    # может пройти Грейд II -> III -> IV.
    if count == 6:
        return (
            "🚀 <b>НОВЫЙ ГРЕЙД</b>\n\n"
            f"<b>6 лидов. {e(name)} переходит на Грейд II.</b>\n\n"
            "<blockquote>💰 <b>230 ₽/ч + 75 ₽ за лид</b>\n"
            "До следующей ступени: <b>2 лида</b></blockquote>"
        )
    if count == 8:
        return (
            "🔥 <b>НОВЫЙ ГРЕЙД</b>\n\n"
            f"<b>8 лидов. {e(name)} переходит на Грейд III.</b>\n\n"
            "<blockquote>💰 <b>240 ₽/ч + 80 ₽ за лид</b>\n"
            "До следующей ступени: <b>3 лида</b></blockquote>"
        )
    return (
        "🏆 <b>МАКСИМАЛЬНЫЙ ГРЕЙД</b>\n\n"
        f"<b>11 лидов. {e(name)} переходит на Грейд IV.</b>\n\n"
        "<blockquote>💰 <b>260 ₽/ч + 90 ₽ за лид</b>\n"
        "Максимальная ступень на сегодня достигнута.</blockquote>"
    )


def personal_record(name: str, current: int, previous: int) -> str:
    return (
        "🏆 <b>ЛИЧНЫЙ РЕКОРД</b>\n\n"
        f"<b>{e(name)} — новый личный рекорд: {current} {plural_leads(current)} за смену.</b>\n\n"
        f"<blockquote>Предыдущий рекорд: <b>{previous}</b>\nНовый рекорд: <b>{current}</b></blockquote>"
    )


def daily_plan(name: str, fact: int, daily_plan_value: float) -> str:
    target = math.ceil(daily_plan_value - 1e-9)
    return (
        "✅ <b>ПЛАН ДНЯ ЗАКРЫТ</b>\n\n"
        f"<b>{e(name)} закрывает дневной план.</b>\n\n"
        f"<blockquote>Результат: <b>{fact} / {target}</b> {plural_leads(target)}</blockquote>"
    )


def operator_month_plan(name: str, fact: int, plan: float, day: str) -> str:
    target = math.ceil(plan - 1e-9)
    d = date.fromisoformat(day)
    last_day = __import__("calendar").monthrange(d.year, d.month)[1]
    early = d.day < last_day
    title = "⚡ <b>ПЛАН ВЫПОЛНЕН ДОСРОЧНО</b>" if early else "🎯 <b>ПЛАН МЕСЯЦА ВЫПОЛНЕН</b>"
    extra = f"\nДо конца месяца: <b>{last_day - d.day} дн.</b>" if early else ""
    return (
        f"{title}\n\n"
        f"<b>{e(name)} выходит на 100% месячного плана.</b>\n\n"
        f"<blockquote>Факт: <b>{fact}</b>\nПлан: <b>{target}</b>{extra}</blockquote>"
    )


def group_month_plan(group_name: str, fact: int, plan: float, day: str) -> str:
    target = math.ceil(plan - 1e-9)
    d = date.fromisoformat(day)
    last_day = __import__("calendar").monthrange(d.year, d.month)[1]
    early = d.day < last_day
    title = "⚡ <b>ГРУППА ЗАКРЫЛА ПЛАН ДОСРОЧНО</b>" if early else "🎯 <b>ПЛАН ГРУППЫ ВЫПОЛНЕН</b>"
    extra = f"\nДо конца месяца: <b>{last_day - d.day} дн.</b>" if early else ""
    return (
        f"{title}\n\n"
        f"<b>Группа «{e(group_name)}» вышла на 100% месячного плана.</b>\n\n"
        f"<blockquote>Факт: <b>{fact}</b>\nПлан: <b>{target}</b>{extra}</blockquote>"
    )


def team_month_plan(company: str, fact: int, plan: float, day: str) -> str:
    target = math.ceil(plan - 1e-9)
    d = date.fromisoformat(day)
    last_day = __import__("calendar").monthrange(d.year, d.month)[1]
    early = d.day < last_day
    title = "🚀 <b>ПЛАН ОТДЕЛА ЗАКРЫТ ДОСРОЧНО</b>" if early else "🎯 <b>ПЛАН ОТДЕЛА ВЫПОЛНЕН</b>"
    extra = f"\nДо конца месяца: <b>{last_day - d.day} дн.</b>" if early else ""
    return (
        f"{title}\n\n"
        f"<b>{e(company)} вышел на 100% месячного плана.</b>\n\n"
        f"<blockquote>Факт: <b>{fact}</b>\nПлан: <b>{target}</b>{extra}</blockquote>"
    )


def group_record(group_name: str, current: int, previous: int) -> str:
    return (
        "🔥 <b>РЕКОРД ГРУППЫ ПОБИТ</b>\n\n"
        f"<b>Группа «{e(group_name)}» установила новый результат — {current} {plural_leads(current)} за день.</b>\n\n"
        f"<blockquote>Предыдущий рекорд: <b>{previous}</b>\nНовый рекорд: <b>{current}</b></blockquote>"
    )


def team_record(company: str, current: int, previous: int) -> str:
    return (
        "🚀 <b>НОВЫЙ РЕКОРД ОТДЕЛА</b>\n\n"
        f"<b>{e(company)} установил лучший результат за всю историю — {current} {plural_leads(current)} за день.</b>\n\n"
        f"<blockquote>Предыдущий рекорд: <b>{previous}</b>\nНовый рекорд: <b>{current}</b></blockquote>"
    )
