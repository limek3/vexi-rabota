from leadup_bot.daily import DayLead, DayShift, bar, build_summary, is_quiet, render, short_name
from leadup_bot.models import Group, Operator, Settings


def op(id, name, hire="2026-09-01", group="g1", plan=None):
    return Operator(id=id, name=name, group_id=group, status="active", hire_date=hire, fire_date="", monthly_plan=plan, deleted_at=None)


OPS = {
    "k": op("k", "Комиссарова Валерия"),
    "s": op("s", "Шумаков Илья"),
    "a": op("a", "Ализаде Рамиль"),
    "d": op("d", "Диникаев Артур"),
}
GROUPS = {"g1": Group(id="g1", name="Группа 1", monthly_plan=0, active=True, deleted_at=None)}
# план отдела 150, рабочие пн–пт: в сентябре 2026 их 22 → план дня 6,8
SETTINGS = Settings(team_plan=150, company_name="Отдел продаж", conv_norm_pct=60)
DAY = "2026-09-25"  # пятница


def leads(op_id, n, status="done", day=DAY):
    return [DayLead(f"{day}T1{i % 10}:00", op_id, "g1", status) for i in range(n)]


def summary(extra_leads=(), extra_shifts=()):
    ls = leads("k", 8) + leads("s", 5) + leads("s", 1, "work") + leads("s", 2, "failed") + list(extra_leads)
    sh = [
        DayShift(DAY, "k", 7.5, "work"),
        DayShift(DAY, "s", 10, "work"),
        DayShift(DAY, "a", 6, "work"),
        DayShift("2026-09-26", "d", 12, "work"),
        DayShift("2026-09-26", "a", 0, "off"),
        *extra_shifts,
    ]
    return build_summary(DAY, ls, sh, OPS, GROUPS, {}, SETTINGS)


def test_only_done_count_work_is_pending():
    s = summary()
    assert s.leads == 13  # 8 + 5 доведённых; «в работе» и «не доведён» не в итоге
    assert s.pending == 1
    assert s.hours == 23.5


def test_top_and_no_leads():
    s = summary()
    assert [x.op.id for x in s.top] == ["k", "s"]
    assert [x.op.id for x in s.no_leads] == ["a"]


def test_day_plan_and_month():
    s = summary(extra_leads=leads("k", 3, day="2026-09-22"))
    assert round(s.day_plan, 2) == round(150 / 22, 2)
    assert s.month_fact == 16
    assert s.week_fact == 16  # 22.09 (пн) — та же неделя
    assert s.workdays_left == 3  # 28, 29, 30 сентября


def test_tomorrow_only_real_shifts():
    s = summary()
    assert [(x.op.id, x.hours) for x in s.tomorrow] == [("d", 12)]


def test_weekend_has_no_day_plan():
    s = build_summary("2026-09-26", leads("d", 4, day="2026-09-26"), [DayShift("2026-09-26", "d", 12, "work")], OPS, GROUPS, {}, SETTINGS)
    assert s.day_plan == 0
    assert "Лиды: <b>4</b>\n" in render(s)  # в выходной — без «при плане»


def test_quiet_day():
    s = build_summary("2026-09-27", [], [], OPS, GROUPS, {}, SETTINGS)
    assert is_quiet(s)


def test_render_marks_and_escapes():
    evil = dict(OPS, x=op("x", "<b>Хакер</b> Иван"))
    s = build_summary(DAY, leads("x", 9), [DayShift(DAY, "x", 8, "work")], evil, GROUPS, {}, SETTINGS)
    text = render(s)
    assert "&lt;b&gt;Хакер&lt;/b&gt;" in text
    assert "Лиды: <b>9</b> при плане 7 — <b>132%</b> ✅" in text
    assert "🏆 Лучший оператор: <b>&lt;b&gt;Хакер&lt;/b&gt; И.</b>" in text


def test_short_name_and_bar():
    assert short_name("Комиссарова Валерия Андреевна") == "Комиссарова В. А."
    assert short_name("Лев") == "Лев"
    assert bar(0.53) == "▰▰▰▰▰▱▱▱▱▱"
    assert bar(2.05) == "▰" * 10


def test_render_format():
    text = render(summary())
    assert text.startswith("📊 <b>Итоги дня · пт, 25 сентября</b>\nГруппа 1\n")
    assert "⏳ Ещё на проверке: 1 лид" in text
    assert "🏆 Лучший оператор: <b>Комиссарова В.</b> — 8 лидов за 7,5 ч" in text
    assert "🥈 Шумаков И. — 5 лидов за 10 ч" in text
    assert "⚠️ <b>Без лидов:</b> Ализаде Р. — 6 ч на смене" in text
    assert "📅 <b>Неделя</b> (21–27.09):" in text
    assert "Завтра на смене: Диникаев А. (12 ч)" in text


def test_top_three_places():
    s = summary(extra_leads=leads("a", 3))
    assert [x.op.id for x in s.top] == ["k", "s", "a"]
    text = render(s)
    assert "🏆 Лучший оператор: <b>Комиссарова В.</b>" in text
    assert "🥈 Шумаков И. — 5 лидов за 10 ч" in text
    assert "🥉 Ализаде Р. — 3 лида за 6 ч" in text


def test_supervisors_not_in_day_summary():
    # супервайзер по роли, по схеме «оклад + бонус за объём группы» и руководитель группы
    ops = dict(
        OPS,
        sv=Operator(id="sv", name="Чудаева Юлия", group_id="g1", status="active", hire_date="2026-09-01", fire_date="", monthly_plan=None, deleted_at=None, role="supervisor"),
        vol=Operator(id="vol", name="Объёмов Иван", group_id="g1", status="active", hire_date="2026-09-01", fire_date="", monthly_plan=None, deleted_at=None, pay_type="sv_volume"),
        lead=op("lead", "Руководов Пётр"),
    )
    groups = {"g1": Group(id="g1", name="Группа 1", monthly_plan=0, active=True, deleted_at=None, supervisor_id="lead")}
    ls = leads("k", 8) + leads("sv", 2)
    sh = [
        DayShift(DAY, "k", 7.5, "work"),
        DayShift(DAY, "sv", 8, "work"),
        DayShift(DAY, "vol", 8, "work"),
        DayShift(DAY, "lead", 8, "work"),
        DayShift("2026-09-26", "sv", 8, "work"),
        DayShift("2026-09-26", "d", 12, "work"),
    ]
    s = build_summary(DAY, ls, sh, ops, groups, {}, SETTINGS)
    assert s.leads == 10  # лиды супервайзера — в общем итоге
    assert s.hours == 7.5  # часы — только операторов на линии
    assert [x.op.id for x in s.top] == ["k"]
    assert s.no_leads == []  # супервайзеры без лидов — не «без лидов»
    assert [x.op.id for x in s.tomorrow] == ["d"]
    assert "Чудаева" not in render(s)


def test_plan_missed_is_marked():
    s = build_summary(DAY, leads("k", 3), [DayShift(DAY, "k", 8, "work")], OPS, GROUPS, {}, SETTINGS)
    assert "Лиды: <b>3</b> при плане 7 — <b>44%</b> ⚠️" in render(s)
