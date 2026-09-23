from __future__ import annotations

import calendar
import html
import math
from datetime import date


def e(value: object) -> str:
    return html.escape(str(value), quote=False)


def n(value: float | int) -> str:
    v = float(value)
    return f"{int(v):,}".replace(",", " ") if v.is_integer() else f"{v:,.1f}".replace(",", " ").replace(".0", "")


def mention(name: str, username: str | None = None, telegram_user_id: int | None = None) -> str:
    """Оператор в тексте уведомления: «@username Фамилия Имя» — Telegram подсветит и уведомит.
    Без username — имя ссылкой tg://user?id=…; Telegram не привязан — просто имя."""
    if username:
        return f"@{e(username)} {e(name)}"
    if telegram_user_id:
        return f'<a href="tg://user?id={int(telegram_user_id)}">{e(name)}</a>'
    return e(name)


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


# Оформление уведомлений везде одно: жирный — только заголовок, текст и цитата — обычным шрифтом.

# Ставка ступени (₽/ч, ₽ за лид) и порог в доведённых лидах за смену.
GRADE_RATES: dict[str, tuple[int, int]] = {"II": (230, 75), "III": (240, 80), "IV": (260, 90)}
GRADE_AT: dict[str, int] = {"II": 6, "III": 8, "IV": 11}
# За один лид до ступени: 5 → II, 7 → III, 10 → IV.
GRADE_SOON: dict[int, str] = {at - 1: g for g, at in GRADE_AT.items()}


def rate_line(grade_no: str) -> str:
    hourly, bonus = GRADE_RATES[grade_no]
    return f"💰 {hourly} ₽/ч + {bonus} ₽ за лид"


def grade(name: str, count: int, who: str | None = None) -> str:
    # Грейд относится только к текущей смене/календарному дню.
    # На следующий день счетчик начинается заново, поэтому оператор снова
    # может пройти Грейд II -> III -> IV.
    who = who or e(name)
    if count == 6:
        return (
            "🚀 <b>НОВЫЙ ГРЕЙД</b>\n\n"
            f"6 лидов. {who} переходит на Грейд II.\n\n"
            f"<blockquote>{rate_line('II')}\n"
            "До следующей ступени: 2 лида</blockquote>"
        )
    if count == 8:
        return (
            "🔥 <b>НОВЫЙ ГРЕЙД</b>\n\n"
            f"8 лидов. {who} переходит на Грейд III.\n\n"
            f"<blockquote>{rate_line('III')}\n"
            "До следующей ступени: 3 лида</blockquote>"
        )
    return (
        "🏆 <b>МАКСИМАЛЬНЫЙ ГРЕЙД</b>\n\n"
        f"11 лидов. {who} переходит на Грейд IV.\n\n"
        f"<blockquote>{rate_line('IV')}\n"
        "Максимальная ступень на сегодня достигнута.</blockquote>"
    )


def grade_soon(name: str, count: int, who: str | None = None) -> str:
    """За один доведённый лид до новой ступени: 5, 7 или 10 лидов."""
    grade_no = GRADE_SOON[count]
    return (
        f"⏳ <b>ЕЩЁ 1 ЛИД ДО ГРЕЙДА {grade_no}</b>\n\n"
        f"{count} {plural_leads(count)}. {who or e(name)} — ещё 1 лид, и откроется Грейд {grade_no}.\n\n"
        f"<blockquote>{rate_line(grade_no)}\n"
        f"Ставка за смену и бонус за каждый лид — с {GRADE_AT[grade_no]}-го лида</blockquote>"
    )


def personal_record(name: str, current: int, previous: int, who: str | None = None) -> str:
    return (
        "🏆 <b>ЛИЧНЫЙ РЕКОРД</b>\n\n"
        f"{who or e(name)} — новый личный рекорд: {current} {plural_leads(current)} за смену.\n\n"
        f"<blockquote>Предыдущий рекорд: {previous}\nНовый рекорд: {current}</blockquote>"
    )


def daily_plan(name: str, fact: int, daily_plan_value: float, who: str | None = None) -> str:
    target = math.ceil(daily_plan_value - 1e-9)
    return (
        "✅ <b>ПЛАН ДНЯ ЗАКРЫТ</b>\n\n"
        f"{who or e(name)} закрывает дневной план.\n\n"
        f"<blockquote>Результат: {fact} / {target} {plural_leads(target)}</blockquote>"
    )


def _month_left(day: str) -> tuple[bool, str]:
    """Досрочно ли (не последний день месяца) и строка «До конца месяца: N дн.»."""
    d = date.fromisoformat(day)
    last_day = calendar.monthrange(d.year, d.month)[1]
    early = d.day < last_day
    return early, (f"\nДо конца месяца: {last_day - d.day} дн." if early else "")


def operator_month_plan(name: str, fact: int, plan: float, day: str, who: str | None = None) -> str:
    target = math.ceil(plan - 1e-9)
    early, extra = _month_left(day)
    title = "⚡ <b>ПЛАН ВЫПОЛНЕН ДОСРОЧНО</b>" if early else "🎯 <b>ПЛАН МЕСЯЦА ВЫПОЛНЕН</b>"
    return (
        f"{title}\n\n"
        f"{who or e(name)} выходит на 100% месячного плана.\n\n"
        f"<blockquote>Факт: {fact}\nПлан: {target}{extra}</blockquote>"
    )


def group_month_plan(group_name: str, fact: int, plan: float, day: str) -> str:
    target = math.ceil(plan - 1e-9)
    early, extra = _month_left(day)
    title = "⚡ <b>ГРУППА ЗАКРЫЛА ПЛАН ДОСРОЧНО</b>" if early else "🎯 <b>ПЛАН ГРУППЫ ВЫПОЛНЕН</b>"
    return (
        f"{title}\n\n"
        f"Группа «{e(group_name)}» вышла на 100% месячного плана.\n\n"
        f"<blockquote>Факт: {fact}\nПлан: {target}{extra}</blockquote>"
    )


def team_month_plan(company: str, fact: int, plan: float, day: str) -> str:
    target = math.ceil(plan - 1e-9)
    early, extra = _month_left(day)
    title = "🚀 <b>ПЛАН ОТДЕЛА ЗАКРЫТ ДОСРОЧНО</b>" if early else "🎯 <b>ПЛАН ОТДЕЛА ВЫПОЛНЕН</b>"
    return (
        f"{title}\n\n"
        f"{e(company)} вышел на 100% месячного плана.\n\n"
        f"<blockquote>Факт: {fact}\nПлан: {target}{extra}</blockquote>"
    )


def group_record(group_name: str, current: int, previous: int) -> str:
    return (
        "🔥 <b>РЕКОРД ГРУППЫ ПОБИТ</b>\n\n"
        f"Группа «{e(group_name)}» установила новый результат — {current} {plural_leads(current)} за день.\n\n"
        f"<blockquote>Предыдущий рекорд: {previous}\nНовый рекорд: {current}</blockquote>"
    )


def team_record(company: str, current: int, previous: int) -> str:
    return (
        "🚀 <b>НОВЫЙ РЕКОРД ОТДЕЛА</b>\n\n"
        f"{e(company)} установил лучший результат за всю историю — {current} {plural_leads(current)} за день.\n\n"
        f"<blockquote>Предыдущий рекорд: {previous}\nНовый рекорд: {current}</blockquote>"
    )


# ── привязка Telegram ──────────────────────────────────────────────────────

LINK_ERRORS = {
    "bad_format": "Код — это 6 цифр из LEADUP.\nПример: <code>/link 482913</code>",
    "not_found": "Такой код не найден или уже заменён новым.\nСоздайте новый код в LEADUP → «Подключить Telegram».",
    "revoked": "Этот код заменён новым.\nИспользуйте последний код из LEADUP.",
    "expired": "Срок действия кода истёк — код живёт 15 минут.\nСоздайте новый код в LEADUP.",
    "used": "Этот код уже использован.\nСоздайте новый код в LEADUP.",
    "telegram_taken": "Этот Telegram уже привязан к другому оператору LEADUP.\n"
                      "Отключите его там или попросите руководителя.",
    "operator_inactive": "Карточка оператора в LEADUP неактивна.\nОбратитесь к руководителю.",
    "rate_limited": "Слишком много неверных попыток.\nПодождите 15 минут и попробуйте снова.",
}


def link_error(reason: str) -> str:
    text = LINK_ERRORS.get(reason, "Не получилось привязать аккаунт. Попробуйте ещё раз чуть позже.")
    return f"⚠️ <b>TELEGRAM НЕ ПОДКЛЮЧЕН</b>\n\n{text}"


def link_help() -> str:
    return (
        "🔗 <b>Привязка к LEADUP</b>\n\n"
        "1. Откройте LEADUP → «Настройки» → «Мой профиль» → блок Telegram → «Подключить Telegram».\n"
        "2. Нажмите «Открыть Vexi» — привязка пройдёт сама.\n\n"
        "Или отправьте код вручную: <code>/link 482913</code>"
    )


def link_private_only() -> str:
    return "🔒 Код привязки отправляйте мне в личные сообщения, а не в общий чат."


def link_success(name: str, tag: str, note: str = "") -> str:
    extra = f"\n\n{note}" if note else ""
    return (
        "✅ <b>TELEGRAM ПОДКЛЮЧЕН</b>\n\n"
        f"{e(name)}\n\n"
        "<blockquote>Аккаунт успешно связан с LEADUP\n"
        f"Текущий тег: {e(tag)}</blockquote>"
        f"{extra}"
    )


# Пояснение под подтверждением привязки, если тег пока не поставить.
LINK_NOTES = {
    "no_chat": "Рабочий чат Vexi ещё не подключен — тег появится сразу после подключения.",
    "not_member": "⚠️ Вы пока не найдены в рабочем чате — тег появится, как только вступите.",
    "admin": "Вы администратор рабочего чата: Telegram не даёт ставить теги администраторам.",
    "no_rights": "Тег появится, когда администратор выдаст Vexi право управлять тегами участников.",
    "error": "Тег сейчас не поставился — Vexi повторит попытку автоматически.",
}


def rights_warning(status: str) -> str:
    if status == "not_admin":
        how = "Сделайте Vexi администратором чата и включите право «Управление тегами» (Manage tags)."
    else:
        how = "Выдайте боту право управления тегами участников (Manage tags) в настройках администраторов."
    return f"⚠️ <b>Vexi не может менять грейды участников.</b>\n\n{how}"


RIGHTS_LABELS = {
    "ok": "доступно",
    "not_admin": "недоступно — бот не администратор",
    "no_manage_tags": "недоступно — нет права «Управление тегами»",
    "no_chat": "недоступно — нет доступа к чату",
    "error": "не удалось проверить",
}
