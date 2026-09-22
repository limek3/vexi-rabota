from leadup_bot.metrics import MetricsCache, PlanResolver
from leadup_bot.models import Group, Lead, MonthPlan, Operator, Settings


def op(**kw):
    base = dict(
        id="op1", name="Иван", group_id="g1", status="active", hire_date="2026-09-01",
        fire_date="", monthly_plan=180, deleted_at=None,
    )
    base.update(kw)
    return Operator(**base)


def test_only_done_leads_count():
    c = MetricsCache()
    c.load([
        Lead("1", "2026-09-22T10:00", "op1", "g1", "work", "1"),
        Lead("2", "2026-09-22T11:00", "op1", "g1", "failed", "2"),
        Lead("3", "2026-09-22T12:00", "op1", "g1", "done", "3"),
    ])
    assert c.op_day_count("op1", "2026-09-22") == 1
    assert c.group_day_count("g1", "2026-09-22") == 1
    assert c.team_day_count("2026-09-22") == 1


def test_work_to_done_increments_count():
    c = MetricsCache()
    c.load([Lead("1", "2026-09-22T10:00", "op1", "g1", "work", "1")])
    assert c.op_day_count("op1", "2026-09-22") == 0
    c.apply(Lead("1", "2026-09-22T10:00", "op1", "g1", "done", "2"))
    assert c.op_day_count("op1", "2026-09-22") == 1


def test_done_to_failed_decrements_count():
    c = MetricsCache()
    c.load([Lead("1", "2026-09-22T10:00", "op1", "g1", "done", "1")])
    c.apply(Lead("1", "2026-09-22T10:00", "op1", "g1", "failed", "2"))
    assert c.op_day_count("op1", "2026-09-22") == 0


def test_operator_month_override_plan():
    o = op()
    r = PlanResolver(
        {o.id: o},
        {"g1": Group("g1", "Авто", 0, True, None)},
        {"2026-09|operator|op1": MonthPlan("2026-09|operator|op1", "2026-09", "operator", "op1", 200)},
        Settings(default_operator_plan=150),
    )
    assert r.operator_plan(o, "2026-09") == 200


def test_group_falls_back_to_sum_of_operator_plans():
    a = op(id="a", monthly_plan=100)
    b = op(id="b", monthly_plan=120)
    r = PlanResolver(
        {"a": a, "b": b},
        {"g1": Group("g1", "Авто", 0, True, None)},
        {},
        Settings(),
    )
    assert r.group_plan("g1", "2026-09") == 220


def test_grade_counts_are_separate_for_each_day():
    c = MetricsCache()
    leads = []
    for i in range(11):
        leads.append(Lead(f"d1-{i}", f"2026-09-22T10:{i:02d}", "op1", "g1", "done", str(i)))
    for i in range(6):
        leads.append(Lead(f"d2-{i}", f"2026-09-23T10:{i:02d}", "op1", "g1", "done", str(100+i)))
    c.load(leads)
    assert c.op_day_count("op1", "2026-09-22") == 11
    assert c.op_day_count("op1", "2026-09-23") == 6
