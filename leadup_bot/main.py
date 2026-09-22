from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from . import messages
from .config import Config
from .db import SupabaseDB
from .events import EventEngine, ReferenceData
from .metrics import MetricsCache

log = logging.getLogger(__name__)


class LeadupBot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db = SupabaseDB(cfg.supabase_url, cfg.supabase_service_key)
        self.cache = MetricsCache()
        self.ref: ReferenceData | None = None
        self.engine: EventEngine | None = None
        self.app = (
            Application.builder()
            .token(cfg.telegram_token)
            .post_init(self.post_init)
            .post_shutdown(self.post_shutdown)
            .build()
        )
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("connect", self.cmd_connect))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("test", self.cmd_test))
        self.monitor_task: asyncio.Task | None = None

    def _admin_ok(self, update: Update) -> bool:
        if not self.cfg.admin_ids:
            return True
        uid = update.effective_user.id if update.effective_user else None
        return uid in self.cfg.admin_ids

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        await update.effective_message.reply_text(
            "🦊 <b>Vexi · LEADUP</b>\n\n"
            "Я слежу за достижениями команды через Supabase и публикую только важные события:\n"
            "• переходы на Грейд II / III / IV\n"
            "• личные рекорды\n"
            "• закрытие дневного плана\n"
            "• 100% месячного плана\n"
            "• рекорды группы и всего отдела\n\n"
            "Для подключения рабочего чата добавь меня в группу и выполни:\n"
            "<code>/connect ТВОЙ_СЕКРЕТ</code>",
            parse_mode=ParseMode.HTML,
        )

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
        await msg.reply_text(
            "✅ <b>ЧАТ ПОДКЛЮЧЕН</b>\n\n"
            "Vexi теперь связан с LEADUP.\n"
            "Новые достижения будут появляться здесь автоматически.",
            parse_mode=ParseMode.HTML,
        )
        log.info("Telegram chat connected: %s", chat.id)
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
            chat_id = await self.db.get_chat_id()
            leads = len(self.cache.leads)
            await msg.reply_text(
                "🟢 <b>VEXI BOT · ONLINE</b>\n\n"
                f"Supabase: <b>OK</b>\n"
                f"Рабочий чат: <b>{chat_id or 'не подключен'}</b>\n"
                f"Лидов в кэше: <b>{leads}</b>\n"
                f"Проверка базы: каждые <b>{self.cfg.poll_interval} сек.</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            log.exception("Status failed")
            await msg.reply_text(f"🔴 Ошибка подключения к базе: <code>{type(exc).__name__}</code>", parse_mode=ParseMode.HTML)

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

    async def send_event(self, text: str) -> None:
        chat_id = await self.db.get_chat_id()
        if not chat_id:
            raise RuntimeError("Telegram work chat is not connected")
        await self.app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )

    async def _load_reference(self) -> ReferenceData:
        ops, groups, plans, settings = await self.db.load_reference()
        return ReferenceData(ops, groups, plans, settings)

    async def bootstrap(self) -> None:
        await self.db.healthcheck()
        leads, ref = await asyncio.gather(self.db.load_leads(), self._load_reference())
        self.cache.load(leads)
        self.ref = ref
        self.engine = EventEngine(self.db, self.cache, ref, self.send_event)

        initialized = await self.db.get_state("initialized_done_v2")
        if initialized != "1":
            seeded = await self.db.seed_events(self.engine.existing_event_keys())
            await self.db.set_state("initialized_done_v2", "1")
            log.info("First startup: seeded %s existing achievements without posting them", seeded)

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

    async def post_init(self, application: Application) -> None:
        await self.bootstrap()
        self.monitor_task = asyncio.create_task(self.monitor(), name="leadup-monitor")
        await application.bot.set_my_commands([
            ("start", "Что умеет Vexi"),
            ("status", "Проверить подключение"),
            ("test", "Отправить тестовое достижение"),
        ])

    async def post_shutdown(self, application: Application) -> None:
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
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
    LeadupBot(cfg).run()


if __name__ == "__main__":
    main()
