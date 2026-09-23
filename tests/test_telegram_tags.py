"""Грейды, теги и привязка Telegram.

Telegram и Supabase подменены фейками с тем же интерфейсом (TagApi / LinkStore),
логика — настоящая: MetricsCache, TagSync, EventEngine, обработчики /link и /start.
SQL-часть привязки (коды, сроки, повторное использование, уникальность Telegram ID)
проверяется на настоящем Postgres: `npm run test:telegram-sql` в корне репозитория.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from telegram.error import BadRequest, Forbidden

from leadup_bot.config import Config
from leadup_bot.events import EventEngine, ReferenceData
from leadup_bot.grades import grade_for, grade_tag, local_today
from leadup_bot.metrics import MetricsCache
from leadup_bot.models import Lead, Operator, PendingUnlink, Settings, TelegramLink
from leadup_bot.tag_sync import TagSync
from leadup_bot.tags import MemberInfo, TagError, classify

MSK = ZoneInfo("Europe/Moscow")
CHAT = -100500
DAY = "2026-09-23"


def at_msk(day: str, hh: int = 12, mm: int = 0) -> datetime:
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=MSK)


def op(op_id: str = "op1", name: str = "Мария Соколова", **kw: Any) -> Operator:
    base: dict[str, Any] = {"id": op_id, "name": name, "group_id": "g1", "status": "active", "hire_date": "2026-09-01",
                            "fire_date": "", "monthly_plan": None, "deleted_at": None}
    base.update(kw)
    return Operator(**base)


def lead(i: int, status: str = "done", day: str = DAY, operator_id: str = "op1") -> Lead:
    return Lead(f"L{i}", f"{day}T10:{i % 60:02d}", operator_id, "g1", status, f"{day}T10:{i % 60:02d}:00Z")


class FakeStore:
    def __init__(self, links: list[TelegramLink] | None = None) -> None:
        self.rows = {x.operator_id: x for x in (links or [])}
        self.state: dict[str, str] = {}
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.unlinks: list[PendingUnlink] = []
        self.finished: dict[int, str] = {}
        self.chat_id: int | None = CHAT

    async def load_links(self) -> list[TelegramLink]:
        # копии, как из базы: TagSync не должен зависеть от общих объектов
        return [TelegramLink(**{k: getattr(x, k) for k in TelegramLink.__slots__}) for x in self.rows.values()]

    async def update_link_state(self, operator_id: str, telegram_user_id: int, fields: dict[str, Any]) -> None:
        row = self.rows.get(operator_id)
        if row and row.telegram_user_id == telegram_user_id:
            for k, v in fields.items():
                if hasattr(row, k):
                    setattr(row, k, v)
        self.updates.append((operator_id, fields))

    async def pending_unlinks(self, limit: int = 50) -> list[PendingUnlink]:
        return [u for u in self.unlinks if u.id not in self.finished]

    async def finish_unlink(self, unlink_id: int, result: str) -> None:
        self.finished[unlink_id] = result

    async def get_state(self, key: str) -> str | None:
        return self.state.get(key)

    async def set_state(self, key: str, value: str) -> None:
        self.state[key] = value

    async def get_chat_id(self) -> int | None:
        return self.chat_id

    # EventEngine: журнал поздравлений
    async def claim_event(self, key: str, typ: str, payload: dict) -> bool:
        if key in self.state:
            return False
        self.state[key] = typ
        return True

    async def release_event(self, key: str) -> None:
        self.state.pop(key, None)


class FakeTelegram:
    """Рабочий чат: участники, их теги и права бота."""

    def __init__(self, rights: str = "ok") -> None:
        self.rights_status = rights
        self.members: dict[int, MemberInfo] = {}
        self.calls: list[tuple] = []
        self.log: list[tuple[str, Any]] | None = None
        self.fail_set: Exception | None = None

    def join(self, user_id: int, tag: str = "", status: str = "member") -> None:
        self.members[user_id] = MemberInfo(status, tag)

    def tag(self, user_id: int) -> str | None:
        m = self.members.get(user_id)
        return m.tag if m else None

    async def bot_rights(self, chat_id: int) -> str:
        self.calls.append(("rights", chat_id))
        return self.rights_status

    async def member(self, chat_id: int, user_id: int) -> MemberInfo:
        self.calls.append(("member", user_id))
        return self.members.get(user_id, MemberInfo("absent"))

    async def set_tag(self, chat_id: int, user_id: int, tag: str) -> None:
        self.calls.append(("set", user_id, tag))
        if self.fail_set:
            raise classify(self.fail_set)
        m = self.members.get(user_id)
        if not m or not m.in_chat:
            raise TagError("not_member")
        if m.status == "admin":
            raise TagError("admin")
        self.members[user_id] = MemberInfo(m.status, tag)
        if self.log is not None:
            self.log.append(("tag", tag))

    def sets(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "set"]


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.t = 0.0

    def __call__(self) -> datetime:
        return self.now


def make_sync(store: FakeStore, tg: FakeTelegram, cache: MetricsCache, ops: dict[str, Operator],
              clock: Clock, alerts: list[str] | None = None) -> TagSync:
    async def on_problem(status: str) -> None:
        if alerts is not None:
            alerts.append(status)

    return TagSync(
        store, tg, cache, lambda: ops, store.get_chat_id, MSK,
        now=clock, clock=lambda: clock.t, on_rights_problem=on_problem, pause=0,
    )


def linked(op_id: str = "op1", tg_id: int = 777, **kw: Any) -> TelegramLink:
    return TelegramLink(operator_id=op_id, telegram_user_id=tg_id, username="maria", **kw)


def run(coro):
    return asyncio.run(coro)


# ── 7–10. Пороги грейдов ────────────────────────────────────────────────────

@pytest.mark.parametrize("count,tag", [
    (0, "Грейд I"), (5, "Грейд I"),               # 7. 0–5
    (6, "Грейд II"), (7, "Грейд II"),             # 8. 6–7
    (8, "Грейд III"), (10, "Грейд III"),          # 9. 8–10
    (11, "Грейд IV"), (25, "Грейд IV"),           # 10. 11+
])
def test_grade_thresholds(count: int, tag: str) -> None:
    assert grade_tag(count) == tag


def test_tags_fit_telegram_limits() -> None:
    for n in range(1, 5):
        tag = grade_tag({1: 0, 2: 6, 3: 8, 4: 11}[n])
        assert len(tag) <= 16
        assert all(ord(ch) < 0x2000 for ch in tag), "без эмодзи"
    assert grade_for(5) == 1 and grade_for(6) == 2


def test_today_uses_app_timezone_not_utc() -> None:
    # 23:30 UTC 22 сентября — в Москве уже 23 сентября
    utc_late = datetime.fromisoformat("2026-09-22T23:30:00+00:00")
    assert local_today(MSK, utc_late) == "2026-09-23"


# ── 11–13. Что входит в расчёт ──────────────────────────────────────────────

def _desired(leads: list[Lead]) -> str:
    cache = MetricsCache()
    cache.load(leads)
    sync = make_sync(FakeStore(), FakeTelegram(), cache, {"op1": op()}, Clock(at_msk(DAY)))
    return sync.desired_tag("op1")


def test_work_leads_do_not_count() -> None:                      # 11
    assert _desired([lead(i, "work") for i in range(12)]) == "Грейд I"


def test_failed_leads_do_not_count() -> None:                    # 12
    assert _desired([lead(i, "failed") for i in range(12)]) == "Грейд I"


def test_done_leads_count() -> None:                             # 13
    mixed = [lead(i, "done") for i in range(6)] + [lead(100 + i, "work") for i in range(5)] + [lead(200, "failed")]
    assert _desired(mixed) == "Грейд II"


def test_other_days_do_not_count() -> None:
    assert _desired([lead(i, "done", day="2026-09-22") for i in range(11)]) == "Грейд I"


def test_fired_operator_gets_no_tag() -> None:
    cache = MetricsCache()
    cache.load([lead(i) for i in range(8)])
    sync = make_sync(FakeStore(), FakeTelegram(), cache, {"op1": op(status="fired")}, Clock(at_msk(DAY)))
    assert sync.desired_tag("op1") == ""


# ── 14. Рост и понижение через поток изменений лидов ────────────────────────

def _engine(store: FakeStore, sync: TagSync, cache: MetricsCache, ops: dict[str, Operator], sent: list) -> EventEngine:
    async def sender(text: str) -> None:
        sent.append(("msg", text))

    async def hook(op_id: str) -> None:
        await sync.sync_operator(op_id, reason="lead_change")

    ref = ReferenceData(ops, {}, {}, Settings())
    return EventEngine(store, cache, ref, sender, on_operator_change=hook)  # type: ignore[arg-type]


def test_tag_changes_before_grade_message_and_goes_down() -> None:
    async def scenario() -> None:
        store, tg, cache = FakeStore([linked()]), FakeTelegram(), MetricsCache()
        tg.join(777)
        ops = {"op1": op()}
        order: list = []
        tg.log = order
        cache.load([lead(i) for i in range(5)] + [lead(5, "work")])
        sync = make_sync(store, tg, cache, ops, Clock(at_msk(DAY)))
        await sync.load()
        engine = _engine(store, sync, cache, ops, order)

        await sync.reconcile_all("startup", verify=True)
        assert tg.tag(777) == "Грейд I"

        # 5 → 6: сначала тег, потом поздравление
        await engine.process_lead_change(lead(5, "done"))
        assert tg.tag(777) == "Грейд II"
        kinds = [k for k, _ in order]
        assert kinds.index("tag") < kinds.index("msg")
        assert "Грейд II" in order[-1][1] and "6 лидов" in order[-1][1]

        for i in (6, 7):                                   # 7 → 8
            await engine.process_lead_change(lead(i, "done"))
        assert tg.tag(777) == "Грейд III"

        # супервайзер поменял доведённый лид на «не доведён»: 8 → 7
        await engine.process_lead_change(lead(7, "failed"))
        assert tg.tag(777) == "Грейд II"

        # 6 → 5 и лид «вернули в работу»
        await engine.process_lead_change(lead(6, "failed"))
        await engine.process_lead_change(lead(5, "work"))
        assert tg.tag(777) == "Грейд I"
        # в базе записан фактический тег
        assert store.rows["op1"].tag_synced == "Грейд I"

    run(scenario())


def test_reassigned_lead_updates_both_operators() -> None:
    async def scenario() -> None:
        store = FakeStore([linked("op1", 777), linked("op2", 888)])
        tg, cache = FakeTelegram(), MetricsCache()
        tg.join(777)
        tg.join(888)
        ops = {"op1": op("op1"), "op2": op("op2", "Иван Петров")}
        cache.load([lead(i) for i in range(6)])
        sync = make_sync(store, tg, cache, ops, Clock(at_msk(DAY)))
        await sync.load()
        engine = _engine(store, sync, cache, ops, [])
        await sync.reconcile_all("startup", verify=True)
        assert (tg.tag(777), tg.tag(888)) == ("Грейд II", "Грейд I")
        await engine.process_lead_change(lead(0, "done", operator_id="op2"))
        assert tg.tag(777) == "Грейд I"

    run(scenario())


# ── 15. Новый день ──────────────────────────────────────────────────────────

def test_new_day_returns_grade_one_without_messages() -> None:
    async def scenario() -> None:
        store, tg, cache = FakeStore([linked()]), FakeTelegram(), MetricsCache()
        tg.join(777)
        cache.load([lead(i) for i in range(11)])
        clock = Clock(at_msk(DAY, 23, 50))
        sync = make_sync(store, tg, cache, {"op1": op()}, clock)
        await sync.load()
        await sync.reconcile_all("startup", verify=True)
        assert tg.tag(777) == "Грейд IV"

        clock.now = at_msk("2026-09-24", 0, 1)
        assert sync.today() == "2026-09-24"
        summary = await sync.reconcile_all("new_day")
        assert summary.changed == 1
        assert tg.tag(777) == "Грейд I"

    run(scenario())


# ── 16. Рестарт: сверка восстанавливает правильный тег ──────────────────────

def test_restart_reconcile_restores_tag() -> None:
    async def scenario() -> None:
        # бот лежал всю ночь: в базе и в чате остался вчерашний «Грейд IV»
        store = FakeStore([linked(tag_synced="Грейд IV", tag_chat_id=CHAT, chat_status="member")])
        tg, cache = FakeTelegram(), MetricsCache()
        tg.join(777, tag="Грейд IV")
        cache.load([lead(i, day="2026-09-22") for i in range(12)] + [lead(50 + i) for i in range(8)])
        sync = make_sync(store, tg, cache, {"op1": op()}, Clock(at_msk(DAY, 9)))
        await sync.load()
        await sync.reconcile_all("startup", verify=True)
        assert tg.tag(777) == "Грейд III"
        assert store.rows["op1"].tag_synced == "Грейд III"

        # участника вручную «переименовали» в чате — ежечасная проверка это найдёт
        tg.join(777, tag="Грейд I")
        await sync.reconcile_all("periodic")
        assert tg.tag(777) == "Грейд I", "без verify чат не трогаем"
        await sync.reconcile_all("verify", verify=True)
        assert tg.tag(777) == "Грейд III"

    run(scenario())


# ── 17. Нет прав у бота — процесс не падает ────────────────────────────────

def test_missing_rights_do_not_crash_and_alert_once() -> None:
    async def scenario() -> None:
        store, tg, cache = FakeStore([linked()]), FakeTelegram(rights="no_manage_tags"), MetricsCache()
        tg.join(777)
        alerts: list[str] = []
        clock = Clock(at_msk(DAY))
        sync = make_sync(store, tg, cache, {"op1": op()}, clock, alerts)
        await sync.load()
        r1 = await sync.reconcile_all("startup", verify=True)
        assert r1.failed == 1 and not tg.sets()
        assert store.rows["op1"].chat_status == "no_rights"
        assert store.state["tag_rights"] == "no_manage_tags"
        clock.t += 120
        await sync.reconcile_all("periodic")
        assert alerts == ["no_manage_tags"], "предупреждение одно, без спама"

        # права выдали — тег встаёт при следующей сверке
        tg.rights_status = "ok"
        clock.t += 120
        await sync.reconcile_all("periodic")
        assert tg.tag(777) == "Грейд I"

    run(scenario())


def test_rights_revoked_mid_flight_is_logged_not_raised() -> None:
    async def scenario() -> None:
        store, tg, cache = FakeStore([linked()]), FakeTelegram(), MetricsCache()
        tg.join(777)
        tg.fail_set = BadRequest("Not enough rights to manage chat member tags")
        sync = make_sync(store, tg, cache, {"op1": op()}, Clock(at_msk(DAY)))
        await sync.load()
        assert await sync.sync_operator("op1", verify=True) == "failed:no_rights"
        tg.fail_set = Forbidden("Forbidden: bot was kicked from the supergroup chat")
        assert (await sync.sync_operator("op1", verify=True)).startswith("failed:")

    run(scenario())


def test_error_classification() -> None:
    assert classify(BadRequest("Not enough rights")).kind == "no_rights"
    assert classify(BadRequest("Bad Request: user not found")).kind == "not_member"
    assert classify(BadRequest("PARTICIPANT_ID_INVALID")).kind == "not_member"
    assert classify(BadRequest("Chat not found")).kind == "no_chat"
    assert classify(BadRequest("TAG_INVALID")).kind == "invalid_tag"
    assert classify(Forbidden("Forbidden: not enough rights")).kind == "no_rights"


# ── 18. Идемпотентность ─────────────────────────────────────────────────────

def test_same_event_twice_is_idempotent() -> None:
    async def scenario() -> None:
        store, tg, cache = FakeStore([linked()]), FakeTelegram(), MetricsCache()
        tg.join(777)
        ops = {"op1": op()}
        sent: list = []
        cache.load([lead(i) for i in range(5)])
        sync = make_sync(store, tg, cache, ops, Clock(at_msk(DAY)))
        await sync.load()
        engine = _engine(store, sync, cache, ops, sent)
        await sync.reconcile_all("startup", verify=True)
        tg.calls.clear()

        await engine.process_lead_change(lead(5, "done"))
        await engine.process_lead_change(lead(5, "done"))   # повтор того же изменения
        assert len(tg.sets()) == 1
        assert len([m for m in sent if "НОВЫЙ ГРЕЙД" in m[1]]) == 1

        # тег уже правильный — сверка не шлёт в Telegram ни одного запроса
        tg.calls.clear()
        await sync.reconcile_all("periodic")
        await sync.reconcile_all("periodic")
        assert tg.calls == []

    run(scenario())


# ── Состояния участника ─────────────────────────────────────────────────────

def test_member_not_in_chat_then_joins() -> None:
    async def scenario() -> None:
        store, tg, cache = FakeStore([linked()]), FakeTelegram(), MetricsCache()
        cache.load([lead(i) for i in range(8)])
        sync = make_sync(store, tg, cache, {"op1": op()}, Clock(at_msk(DAY)))
        await sync.load()
        assert await sync.sync_operator("op1") == "skipped:not_member"
        assert store.rows["op1"].chat_status == "not_member"   # CRM покажет «не найдены в чате»

        tg.join(777)
        await sync.reconcile_all("periodic")                    # not_member перепроверяется каждый раз
        assert tg.tag(777) == "Грейд III"
        assert store.rows["op1"].chat_status == "member"

        tg.members[777] = MemberInfo("left")                    # вышел из чата
        assert await sync.sync_operator("op1", verify=True) == "skipped:not_member"

    run(scenario())


def test_chat_admin_is_not_tagged() -> None:
    async def scenario() -> None:
        store, tg, cache = FakeStore([linked()]), FakeTelegram(), MetricsCache()
        tg.join(777, status="admin")
        sync = make_sync(store, tg, cache, {"op1": op()}, Clock(at_msk(DAY)))
        await sync.load()
        assert await sync.sync_operator("op1") == "skipped:admin"
        assert not tg.sets()

    run(scenario())


def test_chat_not_connected_defers_tag() -> None:
    async def scenario() -> None:
        store, tg, cache = FakeStore([linked()]), FakeTelegram(), MetricsCache()
        store.chat_id = None
        tg.join(777)
        sync = make_sync(store, tg, cache, {"op1": op()}, Clock(at_msk(DAY)))
        await sync.load()
        assert await sync.sync_operator("op1") == "skipped:no_chat"
        assert tg.calls == []
        store.chat_id = CHAT                                    # /connect
        await sync.reconcile_all("connect", verify=True)
        assert tg.tag(777) == "Грейд I"

    run(scenario())


def test_unlink_clears_tag_unless_relinked() -> None:
    async def scenario() -> None:
        store, tg, cache = FakeStore([linked("op2", 888)]), FakeTelegram(), MetricsCache()
        tg.join(777, tag="Грейд II")
        tg.join(888, tag="Грейд I")
        store.unlinks = [
            PendingUnlink(1, "op1", 777, "2026-09-23T09:00:00+00:00"),
            PendingUnlink(2, "op9", 888, "2026-09-23T09:00:00+00:00"),  # этот Telegram уже у op2
        ]
        sync = make_sync(store, tg, cache, {"op2": op("op2")}, Clock(at_msk(DAY)))
        await sync.load()
        assert await sync.process_unlinks() == 2
        assert store.finished == {1: "cleared", 2: "relinked"}
        assert tg.tag(777) == ""
        assert tg.tag(888) == "Грейд I"

    run(scenario())


# ── 5–6. Команды /link и /start link_XXXXXX ─────────────────────────────────

class LinkDB(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.consumed: list[tuple] = []
        self.codes = {"482913": "op1"}

    async def consume_link_code(self, code: str, tg_id: int, username, first, last) -> dict:
        self.consumed.append((code, tg_id, username))
        op_id = self.codes.pop(code, None)
        if not op_id:
            return {"ok": False, "reason": "used"}
        self.rows[op_id] = TelegramLink(op_id, tg_id, username, first, last)
        return {"ok": True, "operator_id": op_id, "operator_name": "Мария Соколова"}


def _bot(db: LinkDB, tg: FakeTelegram, cache: MetricsCache):
    from leadup_bot.main import LeadupBot

    cfg = Config(
        telegram_token="123456:TEST-TOKEN", supabase_url="https://example.supabase.co", supabase_service_key="x",
        setup_secret="s", admin_ids=frozenset(), poll_interval=4, reference_refresh=30, log_level="INFO",
        app_timezone=MSK, bot_username="VexiTestBot", tag_reconcile_interval=300, tag_verify_interval=3600,
    )
    bot = LeadupBot(cfg)
    bot.db = db  # type: ignore[assignment]
    ops = {"op1": op()}
    bot.ref = ReferenceData(ops, {}, {}, Settings())
    bot.tag_sync = make_sync(db, tg, cache, ops, Clock(at_msk(DAY)))
    return bot


def _update(chat_type: str = "private"):
    replies: list[str] = []

    async def reply_text(text: str, **kw: Any) -> None:
        replies.append(text)

    upd = SimpleNamespace(
        effective_message=SimpleNamespace(reply_text=reply_text),
        effective_user=SimpleNamespace(id=777, username="maria", first_name="Мария", last_name="Соколова"),
        effective_chat=SimpleNamespace(type=chat_type, id=777),
    )
    return upd, replies


def test_link_command_links_and_sets_tag() -> None:                 # 5
    async def scenario() -> None:
        db, tg, cache = LinkDB(), FakeTelegram(), MetricsCache()
        tg.join(777)
        cache.load([lead(i) for i in range(6)])
        bot = _bot(db, tg, cache)
        upd, replies = _update()
        await bot.cmd_link(upd, SimpleNamespace(args=["482913"]))
        assert db.consumed == [("482913", 777, "maria")]
        assert tg.tag(777) == "Грейд II"
        assert "TELEGRAM ПОДКЛЮЧЕН" in replies[-1]
        assert "Мария Соколова" in replies[-1] and "Грейд II" in replies[-1]

        # код одноразовый: второй раз — отказ
        await bot.cmd_link(upd, SimpleNamespace(args=["482913"]))
        assert "уже использован" in replies[-1]

    run(scenario())


def test_start_deeplink_payload_links() -> None:                   # 6
    async def scenario() -> None:
        db, tg, cache = LinkDB(), FakeTelegram(), MetricsCache()
        bot = _bot(db, tg, cache)
        upd, replies = _update()
        await bot.cmd_start(upd, SimpleNamespace(args=["link_482913"]))
        assert db.consumed == [("482913", 777, "maria")]
        assert "TELEGRAM ПОДКЛЮЧЕН" in replies[-1]
        # человека ещё нет в рабочем чате — подтверждение честно об этом говорит
        assert "не найдены в рабочем чате" in replies[-1]

        await bot.cmd_start(upd, SimpleNamespace(args=[]))
        assert "Vexi · LEADUP" in replies[-1]
        assert len(db.consumed) == 1

    run(scenario())


def test_link_in_group_is_refused_without_consuming() -> None:
    async def scenario() -> None:
        db = LinkDB()
        bot = _bot(db, FakeTelegram(), MetricsCache())
        upd, replies = _update("supergroup")
        await bot.cmd_link(upd, SimpleNamespace(args=["482913"]))
        assert db.consumed == []
        assert "личные сообщения" in replies[-1]

    run(scenario())


def test_link_without_code_shows_help() -> None:
    async def scenario() -> None:
        bot = _bot(LinkDB(), FakeTelegram(), MetricsCache())
        upd, replies = _update()
        await bot.cmd_link(upd, SimpleNamespace(args=[]))
        assert "/link 482913" in replies[-1]

    run(scenario())


# ── Старт процесса: bootstrap целиком ───────────────────────────────────────

class BootDB(LinkDB):
    """Supabase на старте: лиды, справочники, журнал, привязки."""

    def __init__(self, leads: list[Lead], links: list[TelegramLink]) -> None:
        super().__init__()
        self.leads = leads
        self.rows = {x.operator_id: x for x in links}
        self.state["initialized_done_v2"] = "1"
        self.available = True

    async def healthcheck(self) -> None:
        return None

    async def load_leads(self) -> list[Lead]:
        return self.leads

    async def load_reference(self):
        return {"op1": op()}, {}, {}, Settings()

    async def seed_events(self, items) -> int:
        return 0

    async def telegram_links_available(self) -> bool:
        return self.available


def test_bootstrap_restores_tags_before_missed_congratulations(monkeypatch: pytest.MonkeyPatch) -> None:  # 16
    import leadup_bot.main as main_mod

    async def scenario() -> None:
        tg = FakeTelegram()
        tg.join(777, tag="Грейд IV")           # вчерашний тег пережил ночь
        order: list = []
        tg.log = order
        today = local_today(MSK)
        db = BootDB([lead(i, day=today) for i in range(6)],
                    [linked(tag_synced="Грейд IV", tag_chat_id=CHAT, chat_status="member")])
        monkeypatch.setattr(main_mod, "TelegramTagApi", lambda bot: tg)
        bot = _bot(db, tg, MetricsCache())
        bot.tag_sync = None
        bot.cache = MetricsCache()

        async def send(text: str) -> None:
            order.append(("msg", text))

        monkeypatch.setattr(bot, "send_event", send)
        await bot.bootstrap()
        assert tg.tag(777) == "Грейд II"
        assert db.rows["op1"].tag_synced == "Грейд II"
        kinds = [k for k, _ in order]
        assert kinds and kinds[0] == "tag", "сначала тег, потом пропущенные поздравления"
        assert any("Грейд II" in t for k, t in order if k == "msg")

    run(scenario())


def test_bootstrap_without_migration_keeps_old_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        db = BootDB([lead(i) for i in range(3)], [])
        db.available = False
        bot = _bot(db, FakeTelegram(), MetricsCache())
        bot.tag_sync = None
        bot.cache = MetricsCache()
        await bot.bootstrap()
        assert bot.tag_sync is None and bot.engine is not None
        upd, replies = _update()
        await bot.cmd_link(upd, SimpleNamespace(args=["482913"]))
        assert "ещё не включена" in replies[-1]
        assert db.consumed == []

    run(scenario())
