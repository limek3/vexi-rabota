from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import messages
from .config import Config
from .db import SupabaseDB
from .events import EventEngine, ReferenceData
from .metrics import MetricsCache
from .tag_sync import TagSync
from .tags import RIGHTS_MISSING, RIGHTS_OK, TelegramTagApi

log = logging.getLogger(__name__)

LINK_PAYLOAD = re.compile(r"^link_([0-9]{6})$")
CHAT_ID_TTL = 30.0
TAG_TICK_SECONDS = 15
RIGHTS_ALERT_REPEAT = timedelta(hours=24)


def mask_code(code: str) -> str:
    """В логах — только первые 2 символа кода."""
    return (code[:2] + "****") if len(code) >= 4 else "****"


class LeadupBot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db = SupabaseDB(cfg.supabase_url, cfg.supabase_service_key)
        self.cache = MetricsCache()
        self.ref: ReferenceData | None = None
        self.engine: EventEngine | None = None
        self.tag_sync: TagSync | None = None
        self.bot_username: str = cfg.bot_username
        self._chat_cache: tuple[int | None, float] | None = None
        self.app = (
            Application.builder()
            .token(cfg.telegram_token)
            .post_init(self.post_init)
            .post_shutdown(self.post_shutdown)
            .build()
        )
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("link", self.cmd_link))
        self.app.add_handler(CommandHandler("connect", self.cmd_connect))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("test", self.cmd_test))
        self.app.add_handler(ChatMemberHandler(self.on_chat_member, ChatMemberHandler.CHAT_MEMBER))
        self.app.add_handler(ChatMemberHandler(self.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
        self.app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, self.on_new_members))
        self.monitor_task: asyncio.Task | None = None
        self.tag_task: asyncio.Task | None = None

    def _admin_ok(self, update: Update) -> bool:
        if not self.cfg.admin_ids:
            return True
        uid = update.effective_user.id if update.effective_user else None
        return uid in self.cfg.admin_ids

    async def _chat_id(self) -> int | None:
        """ID рабочего чата с коротким кэшем: тег проверяется на каждое изменение лида."""
        now = asyncio.get_running_loop().time()
        if self._chat_cache and now - self._chat_cache[1] < CHAT_ID_TTL:
            return self._chat_cache[0]
        chat_id = await self.db.get_chat_id()
        self._chat_cache = (chat_id, now)
        return chat_id

    # ── команды ────────────────────────────────────────────────────────────
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        # t.me/<bot>?start=link_482913 — привязка без ручного ввода кода
        payload = context.args[0] if context.args else ""
        m = LINK_PAYLOAD.match(payload)
        if m:
            await self._link(update, m.group(1))
            return
        await update.effective_message.reply_text(
            "🦊 <b>Vexi · LEADUP</b>\n\n"
            "Я слежу за достижениями команды через Supabase и публикую только важные события:\n"
            "• переходы на Грейд II / III / IV\n"
            "• личные рекорды\n"
            "• закрытие дневного плана\n"
            "• 100% месячного плана\n"
            "• рекорды группы и всего отдела\n\n"
            "Привяжите свой аккаунт LEADUP — и я буду ставить ваш дневной грейд тегом в рабочем чате:\n"
            "<code>/link КОД</code> (код — в LEADUP, блок Telegram)\n\n"
            "Для подключения рабочего чата добавь меня в группу и выполни:\n"
            "<code>/connect ТВОЙ_СЕКРЕТ</code>",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        code = context.args[0] if context.args else ""
        if not code:
            await msg.reply_text(messages.link_help(), parse_mode=ParseMode.HTML)
            return
        await self._link(update, code)

    async def _link(self, update: Update, code: str) -> None:
        msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if not msg or not user or not chat:
            return
        if chat.type != "private":
            await msg.reply_text(messages.link_private_only())
            return
        if not self.tag_sync:
            await msg.reply_text("⚠️ Привязка Telegram ещё не включена в базе LEADUP. Сообщите руководителю.")
            log.warning("telegram.link.unavailable telegram_user_id=%s", user.id)
            return

        code = code.strip()
        try:
            res = await self.db.consume_link_code(code, user.id, user.username, user.first_name, user.last_name)
        except Exception:
            log.exception("telegram.link.error telegram_user_id=%s code=%s", user.id, mask_code(code))
            await msg.reply_text("⚠️ Не получилось связаться с LEADUP. Попробуйте ещё раз через минуту.")
            return
        if not res.get("ok"):
            reason = str(res.get("reason") or "unknown")
            log.info("telegram.link.rejected telegram_user_id=%s reason=%s code=%s", user.id, reason, mask_code(code))
            await msg.reply_text(messages.link_error(reason), parse_mode=ParseMode.HTML)
            return

        op_id = str(res["operator_id"])
        op = self.ref.operators.get(op_id) if self.ref else None
        name = str(res.get("operator_name") or (op.name if op else "Оператор"))
        try:
            await self.tag_sync.load()
        except Exception:
            log.exception("telegram.link.reload_failed operator_id=%s", op_id)
        # тег — сразу, до ответа: человек видит в подтверждении то, что уже стоит в чате
        result = await self.tag_sync.sync_operator(op_id, verify=True, reason="link")
        link = self.tag_sync.links.get(op_id)
        tag = self.tag_sync.desired_tag(op_id) or "—"
        note = ""
        if result not in ("changed", "unchanged") and link:
            note = messages.LINK_NOTES.get(link.chat_status, messages.LINK_NOTES["error"])
        log.info("telegram.link.success operator_id=%s telegram_user_id=%s repeat=%s tag=%s tag_result=%s",
                 op_id, user.id, bool(res.get("repeat")), tag, result)
        await msg.reply_text(messages.link_success(name, tag, note), parse_mode=ParseMode.HTML)

        if res.get("previous_telegram_user_id"):
            try:
                await self.tag_sync.process_unlinks()  # снять тег со старого Telegram этой карточки
            except Exception:
                log.exception("telegram.unlink.process_failed")

    async def cmd_connect(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        chat = update.effective_chat
        if not msg or not chat:
            return
        if not self._admin_ok(update):
            await msg.reply_text("⛔ Недостаточно прав.")
            return
        secret = context.args[0] if context.args else ""
        if not secrets.compare_digest(secret, self.cfg.setup_secret):
            await msg.reply_text("⛔ Неверный секрет подключения.")
            return
        if chat.type not in ("group", "supergroup"):
            await msg.reply_text("Добавь меня в рабочую группу и выполни команду /connect там.")
            return
        await self.db.set_chat_id(chat.id)
        self._chat_cache = (chat.id, asyncio.get_running_loop().time())
        rights_line = ""
        if self.tag_sync:
            self.tag_sync.invalidate_rights()
            rights = await self.tag_sync.rights(chat.id, force=True)
            rights_line = f"\nУправление тегами: <b>{messages.RIGHTS_LABELS.get(rights, rights)}</b>"
        await msg.reply_text(
            "✅ <b>ЧАТ ПОДКЛЮЧЕН</b>\n\n"
            "Vexi теперь связан с LEADUP.\n"
            "Новые достижения будут появляться здесь автоматически."
            f"{rights_line}",
            parse_mode=ParseMode.HTML,
        )
        log.info("Telegram chat connected: %s", chat.id)
        if self.tag_sync:
            # привязки, сделанные до подключения чата, получают теги сейчас
            await self.tag_sync.reconcile_all("connect", verify=True)
        if self.engine and self.cache.team_day:
            try:
                await self.engine.reconcile_day(max(self.cache.team_day))
            except Exception:
                log.exception("Reconcile after /connect failed")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._admin_ok(update):
            await msg.reply_text("⛔ Недостаточно прав.")
            return
        try:
            await self.db.healthcheck()
        except Exception as exc:
            log.exception("Status failed")
            await msg.reply_text(f"🔴 Ошибка подключения к базе: <code>{type(exc).__name__}</code>", parse_mode=ParseMode.HTML)
            return
        chat_id = await self.db.get_chat_id()
        self._chat_cache = (chat_id, asyncio.get_running_loop().time())
        lines = [
            "🟢 <b>VEXI BOT · ONLINE</b>\n",
            f"Telegram чат: <b>{'подключен' if chat_id else 'не подключен'}</b>",
            "Supabase: <b>подключен</b>",
        ]
        rights = None
        if not self.tag_sync:
            lines.append("Управление тегами: <b>выключено — не выполнена миграция привязки Telegram</b>")
        else:
            if chat_id:
                rights = await self.tag_sync.rights(chat_id, force=True)
                lines.append(f"Управление тегами: <b>{messages.RIGHTS_LABELS.get(rights, rights)}</b>")
            else:
                lines.append("Управление тегами: <b>сначала подключите чат через /connect</b>")
            links = self.tag_sync.links.values()
            lines.append(f"Привязано операторов: <b>{len(self.tag_sync.links)}</b>")
            not_in_chat = sum(1 for x in links if x.chat_status == "not_member")
            if not_in_chat:
                lines.append(f"Не найдены в чате: <b>{not_in_chat}</b>")
        lines.append(f"Лидов в кэше: <b>{len(self.cache.leads)}</b>")
        lines.append(f"Проверка базы: каждые <b>{self.cfg.poll_interval} сек.</b>")
        text = "\n".join(lines)
        if rights in RIGHTS_MISSING:
            text += "\n\n" + messages.rights_warning(rights)
        await msg.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._admin_ok(update):
            await msg.reply_text("⛔ Недостаточно прав.")
            return
        chat_id = await self.db.get_chat_id()
        if not chat_id:
            await msg.reply_text("Сначала подключи рабочий чат через /connect.")
            return
        await self.app.bot.send_message(
            chat_id=chat_id,
            text=messages.grade("Тестовый оператор", 6),
            parse_mode=ParseMode.HTML,
        )
        await msg.reply_text("✅ Тестовое сообщение отправлено в подключенный чат.")

    # ── участники рабочего чата ────────────────────────────────────────────
    async def _sync_member(self, chat_id: int, user_id: int, reason: str) -> None:
        if not self.tag_sync or chat_id != await self._chat_id():
            return
        link = self.tag_sync.by_telegram_id(user_id)
        if link:
            await self.tag_sync.sync_operator(link.operator_id, verify=True, reason=reason)

    async def on_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cmu = update.chat_member
        if cmu:
            await self._sync_member(cmu.chat.id, cmu.new_chat_member.user.id, "chat_member")

    async def on_new_members(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not msg.new_chat_members:
            return
        for user in msg.new_chat_members:
            await self._sync_member(msg.chat_id, user.id, "joined")

    async def on_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Боту выдали или забрали права в рабочем чате — сразу пересчитать теги."""
        cmu = update.my_chat_member
        if not cmu or not self.tag_sync or cmu.chat.id != await self._chat_id():
            return
        self.tag_sync.invalidate_rights()
        rights = await self.tag_sync.rights(cmu.chat.id, force=True)
        if rights == RIGHTS_OK:
            await self.tag_sync.reconcile_all("bot_rights", verify=True)

    async def _rights_alert(self, status: str) -> None:
        """Предупреждение в рабочий чат, что тегами управлять нельзя. Не чаще раза в сутки на статус."""
        chat_id = await self._chat_id()
        if not chat_id:
            return
        now = datetime.now(timezone.utc)
        last = await self.db.get_state("tag_rights_alert") or ""
        last_status, _, last_at = last.partition("|")
        try:
            recent = last_status == status and now - datetime.fromisoformat(last_at) < RIGHTS_ALERT_REPEAT
        except ValueError:
            recent = False
        if recent:
            return
        await self.app.bot.send_message(chat_id=chat_id, text=messages.rights_warning(status), parse_mode=ParseMode.HTML)
        await self.db.set_state("tag_rights_alert", f"{status}|{now.isoformat()}")
        log.warning("telegram.tag.rights_alert_sent chat_id=%s status=%s", chat_id, status)

    # ── события лидов ──────────────────────────────────────────────────────
    async def send_event(self, text: str) -> None:
        chat_id = await self.db.get_chat_id()
        if not chat_id:
            raise RuntimeError("Telegram work chat is not connected")
        await self.app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )

    async def on_operator_change(self, operator_id: str) -> None:
        if self.tag_sync:
            await self.tag_sync.sync_operator(operator_id, reason="lead_change")

    async def _load_reference(self) -> ReferenceData:
        ops, groups, plans, settings = await self.db.load_reference()
        return ReferenceData(ops, groups, plans, settings)

    async def _enable_tags(self) -> bool:
        """Включить теги, если миграция привязки уже выполнена. Без неё бот работает как раньше."""
        if self.tag_sync:
            return True
        try:
            if not await self.db.telegram_links_available():
                return False
            sync = TagSync(
                self.db, TelegramTagApi(self.app.bot), self.cache,
                lambda: self.ref.operators if self.ref else {},
                self._chat_id, self.cfg.app_timezone, on_rights_problem=self._rights_alert,
            )
            await sync.load()
        except Exception:
            log.exception("telegram.links.enable_failed")
            return False
        self.tag_sync = sync
        log.info("telegram.links.enabled linked=%s timezone=%s", len(sync.links), self.cfg.app_timezone.key)
        return True

    async def bootstrap(self) -> None:
        await self.db.healthcheck()
        leads, ref = await asyncio.gather(self.db.load_leads(), self._load_reference())
        self.cache.load(leads)
        self.ref = ref
        self.engine = EventEngine(self.db, self.cache, ref, self.send_event, on_operator_change=self.on_operator_change)

        initialized = await self.db.get_state("initialized_done_v2")
        if initialized != "1":
            seeded = await self.db.seed_events(self.engine.existing_event_keys())
            await self.db.set_state("initialized_done_v2", "1")
            log.info("First startup: seeded %s existing achievements without posting them", seeded)

        # Теги — раньше пропущенных поздравлений: после рестарта сначала восстанавливаем
        # фактические грейды в чате (в т.ч. «Грейд I» нового дня), потом догоняем сообщения.
        if await self._enable_tags() and self.tag_sync:
            try:
                await self.tag_sync.reconcile_all("startup", verify=True)
                await self.tag_sync.process_unlinks()
            except Exception:
                log.exception("telegram.reconcile.startup_failed")

        # Reconcile the latest active day. On the first ever run all existing keys were seeded above,
        # so this remains silent. On later Railway restarts it restores achievements that happened
        # while the process was offline.
        if self.cache.team_day and await self.db.get_chat_id():
            await self.engine.reconcile_day(max(self.cache.team_day))

        latest = max((x.updated_at for x in leads if x.updated_at), default=datetime.now(timezone.utc).isoformat())
        await self.db.set_state("last_lead_updated_at", latest)
        log.info("Loaded %s leads, %s operators, %s groups", len(leads), len(ref.operators), len(ref.groups))

    async def monitor(self) -> None:
        assert self.engine is not None
        cursor = await self.db.get_state("last_lead_updated_at") or datetime.now(timezone.utc).isoformat()
        last_ref = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                now = loop.time()
                if now - last_ref >= self.cfg.reference_refresh:
                    ref = await self._load_reference()
                    self.ref = ref
                    self.engine.update_reference(ref)
                    last_ref = now

                # Until /connect is completed, keep the cursor untouched so achievements
                # created after the bot started can still be delivered once the chat is linked.
                if not await self.db.get_chat_id():
                    await asyncio.sleep(self.cfg.poll_interval)
                    continue

                changes = await self.db.changed_leads(cursor)
                for lead in changes:
                    await self.engine.process_lead_change(lead)
                    if lead.updated_at > cursor:
                        cursor = lead.updated_at
                if changes:
                    await self.db.set_state("last_lead_updated_at", cursor)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Monitor loop failed; will retry")
            await asyncio.sleep(self.cfg.poll_interval)

    async def tag_maintenance(self) -> None:
        """Фоновая сверка тегов: смена дня по APP_TIMEZONE, периодическая сверка, отвязки.

        Не зависит от одного «крона в полночь»: день сравнивается на каждом тике, а после
        рестарта/простоя сверка идёт в bootstrap — вчерашний «Грейд IV» утром не останется.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        last_links = last_reconcile = last_verify = started
        last_probe = 0.0
        last_day = self.tag_sync.today() if self.tag_sync else ""
        while True:
            await asyncio.sleep(TAG_TICK_SECONDS)
            try:
                now = loop.time()
                if not self.tag_sync:
                    # миграцию выполнили на ходу — включаемся без рестарта
                    if now - last_probe >= 60:
                        last_probe = now
                        if await self._enable_tags() and self.tag_sync:
                            last_day = self.tag_sync.today()
                            await self.tag_sync.reconcile_all("enabled", verify=True)
                    continue
                sync = self.tag_sync
                if now - last_links >= self.cfg.reference_refresh:
                    await sync.load()  # отвязки и переподключения из CRM
                    await sync.process_unlinks()
                    last_links = now
                day = sync.today()
                if day != last_day:
                    log.info("telegram.day.rollover from=%s to=%s", last_day, day)
                    await sync.reconcile_all("new_day")
                    last_day = day
                    last_reconcile = now
                elif now - last_verify >= self.cfg.tag_verify_interval:
                    await sync.reconcile_all("verify", verify=True)
                    last_verify = last_reconcile = now
                elif now - last_reconcile >= self.cfg.tag_reconcile_interval:
                    await sync.reconcile_all("periodic")
                    last_reconcile = now
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram.reconcile.loop_failed; will retry")

    async def post_init(self, application: Application) -> None:
        try:
            me = await application.bot.get_me()
            self.bot_username = self.cfg.bot_username or (me.username or "")
            if self.bot_username:
                # CRM строит ссылку t.me/<bot>?start=link_XXXXXX по этому значению
                await self.db.set_state("bot_username", self.bot_username)
        except Exception:
            log.exception("Cannot resolve bot username")
        await self.bootstrap()
        self.monitor_task = asyncio.create_task(self.monitor(), name="leadup-monitor")
        self.tag_task = asyncio.create_task(self.tag_maintenance(), name="leadup-tags")
        await application.bot.set_my_commands([
            ("start", "Что умеет Vexi"),
            ("link", "Привязать аккаунт LEADUP"),
            ("status", "Проверить подключение"),
            ("test", "Отправить тестовое достижение"),
        ])

    async def post_shutdown(self, application: Application) -> None:
        for task in (self.monitor_task, self.tag_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self.db.close()

    def run(self) -> None:
        self.app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


def main() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # httpx на INFO пишет каждый URL запроса, а в URL Bot API — токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    LeadupBot(cfg).run()


if __name__ == "__main__":
    main()
