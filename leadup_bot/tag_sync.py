"""Синхронизация пользовательских тегов «Грейд I–IV» в рабочем чате.

Источник правды — число ДОВЕДЁННЫХ лидов оператора за сегодня (APP_TIMEZONE) в MetricsCache.
Состояние «какой тег уже стоит» хранится в operator_telegram.tag_synced, поэтому после
рестарта бот не дёргает Telegram зря, а сверка (reconcile) догоняет всё пропущенное:
новый день, простой ночью, редеплой, вход человека в чат, временные ошибки API.

Идемпотентность: если в базе записан тот же тег, что положен сейчас, и человек в чате —
запросов к Telegram нет вовсе. Раз в TAG_VERIFY_SECONDS теги перепроверяются getChatMember.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any, Protocol

from .grades import grade_tag, local_today, tag_slug
from .metrics import MetricsCache
from .models import Operator, PendingUnlink, TelegramLink
from .tags import (
    RIGHTS_MISSING,
    RIGHTS_NO_CHAT,
    RIGHTS_OK,
    MemberInfo,
    TagApi,
    TagError,
    classify,
)

log = logging.getLogger(__name__)

RIGHTS_CACHE_SECONDS = 60.0
UNLINK_RETRY_DAYS = 7
# пауза между запросами к Telegram при массовой сверке — не упираться в лимиты
RECONCILE_PAUSE = 0.05


class LinkStore(Protocol):
    async def load_links(self) -> list[TelegramLink]: ...
    async def update_link_state(self, operator_id: str, telegram_user_id: int, fields: dict[str, Any]) -> None: ...
    async def pending_unlinks(self, limit: int = 50) -> list[PendingUnlink]: ...
    async def finish_unlink(self, unlink_id: int, result: str) -> None: ...
    async def get_state(self, key: str) -> str | None: ...
    async def set_state(self, key: str, value: str) -> None: ...


@dataclass(slots=True)
class ReconcileSummary:
    total: int = 0
    changed: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def add(self, result: str) -> None:
        self.total += 1
        kind = result.split(":", 1)[0]
        if kind == "changed":
            self.changed += 1
        elif kind == "unchanged":
            self.unchanged += 1
        elif kind == "failed":
            self.failed += 1
            self.reasons[result] = self.reasons.get(result, 0) + 1
        else:
            self.skipped += 1
            self.reasons[result] = self.reasons.get(result, 0) + 1


class TagSync:
    def __init__(
        self,
        store: LinkStore,
        api: TagApi,
        cache: MetricsCache,
        operators: Callable[[], dict[str, Operator]],
        chat_id: Callable[[], Awaitable[int | None]],
        tz: tzinfo,
        *,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] | None = None,
        on_rights_problem: Callable[[str], Awaitable[None]] | None = None,
        pause: float = RECONCILE_PAUSE,
    ) -> None:
        self.store = store
        self.api = api
        self.cache = cache
        self.operators = operators
        self.chat_id = chat_id
        self.tz = tz
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self.on_rights_problem = on_rights_problem
        self.pause = pause
        self.links: dict[str, TelegramLink] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._rights: tuple[int, str, float] | None = None  # (chat_id, status, checked_at)
        self._reconcile_lock = asyncio.Lock()

    # ── состояние ──────────────────────────────────────────────────────────
    def today(self) -> str:
        return local_today(self.tz, self._now())

    async def load(self) -> None:
        links = await self.store.load_links()
        self.links = {x.operator_id: x for x in links}

    def by_telegram_id(self, telegram_user_id: int) -> TelegramLink | None:
        return next((x for x in self.links.values() if x.telegram_user_id == telegram_user_id), None)

    def desired_tag(self, operator_id: str) -> str:
        """Тег, который должен стоять прямо сейчас. Удалённой/уволенной карточке — никакого."""
        op = self.operators().get(operator_id)
        if not op or op.deleted_at or op.status == "fired":
            return ""
        return grade_tag(self.cache.op_day_count(operator_id, self.today()))

    def _lock(self, operator_id: str) -> asyncio.Lock:
        return self._locks.setdefault(operator_id, asyncio.Lock())

    async def _save(self, link: TelegramLink, **changes: Any) -> None:
        """Записать состояние в базу, только если оно поменялось. Ошибку базы логируем — тег
        в Telegram уже стоит, а при следующей сверке состояние перезапишется."""
        now = self._now().isoformat()
        diff = {k: v for k, v in changes.items() if getattr(link, k) != v}
        if not diff:
            return
        fields: dict[str, Any] = {}
        for k, v in diff.items():
            fields[k] = v
        if "tag_synced" in diff or "tag_chat_id" in diff:
            fields["tag_synced_at"] = now
        if "chat_status" in diff:
            fields["chat_checked_at"] = now
        try:
            await self.store.update_link_state(link.operator_id, link.telegram_user_id, fields)
        except Exception:
            log.exception("telegram.link.state_save_failed operator_id=%s", link.operator_id)
            return
        for k, v in diff.items():
            setattr(link, k, v)

    # ── права бота ─────────────────────────────────────────────────────────
    def invalidate_rights(self) -> None:
        self._rights = None

    async def rights(self, chat_id: int, *, force: bool = False) -> str:
        now = self._clock()
        if not force and self._rights and self._rights[0] == chat_id and now - self._rights[2] < RIGHTS_CACHE_SECONDS:
            return self._rights[1]
        try:
            status = await self.api.bot_rights(chat_id)
        except Exception as exc:  # noqa: BLE001 — сбой проверки прав не должен ронять синхронизацию
            err = classify(exc)
            log.warning("telegram.tag.rights_check_failed chat_id=%s reason=%s", chat_id, err.kind)
            status = "error"
        prev = self._rights[1] if self._rights else await self._stored_rights()
        self._rights = (chat_id, status, now)
        if status != prev:
            log.info("telegram.tag.rights chat_id=%s status=%s previous=%s", chat_id, status, prev)
            try:
                await self.store.set_state("tag_rights", status)
            except Exception:
                log.exception("telegram.tag.rights_state_save_failed")
            if status in RIGHTS_MISSING and self.on_rights_problem:
                try:
                    await self.on_rights_problem(status)
                except Exception:
                    log.exception("telegram.tag.rights_alert_failed")
        return status

    async def _stored_rights(self) -> str | None:
        try:
            return await self.store.get_state("tag_rights")
        except Exception:  # noqa: BLE001 — нет состояния в базе, значит права ещё не проверялись
            return None

    # ── один оператор ──────────────────────────────────────────────────────
    async def sync_operator(self, operator_id: str, *, verify: bool = False, reason: str = "change") -> str:
        """Поставить оператору положенный тег. Никогда не бросает исключений.

        Результат: changed · unchanged · skipped:<почему> · failed:<почему>.
        """
        link = self.links.get(operator_id)
        if not link:
            return "skipped:not_linked"
        try:
            async with self._lock(operator_id):
                return await self._sync_locked(link, verify=verify, reason=reason)
        except Exception as exc:  # последняя страховка: процесс бота не должен падать из-за тегов
            err = classify(exc)
            log.exception("telegram.tag.failed operator_id=%s reason=%s", operator_id, err.kind)
            return f"failed:{err.kind}"

    async def _sync_locked(self, link: TelegramLink, *, verify: bool, reason: str) -> str:
        if self.links.get(link.operator_id) is not link:
            return "skipped:not_linked"  # пока ждали блокировку — отвязали или переподключили
        chat_id = await self.chat_id()
        if not chat_id:
            await self._save(link, chat_status="no_chat")
            return "skipped:no_chat"

        desired = self.desired_tag(link.operator_id)
        known_member = link.chat_status == "member" and link.tag_chat_id == chat_id
        if not verify and known_member and link.tag_synced == desired:
            return "unchanged"

        rights = await self.rights(chat_id)
        if rights != RIGHTS_OK:
            status = "no_chat" if rights == RIGHTS_NO_CHAT else "no_rights" if rights in RIGHTS_MISSING else link.chat_status
            await self._save(link, chat_status=status, last_error=f"rights:{rights}")
            log.warning("telegram.tag.failed operator_id=%s telegram_user_id=%s reason=rights:%s",
                        link.operator_id, link.telegram_user_id, rights)
            return f"failed:rights_{rights}"

        before = link.tag_synced if known_member else None
        if verify or not known_member:
            member = await self._member(chat_id, link)
            if member is None:
                return "failed:member_lookup"
            if not member.in_chat:
                await self._save(link, chat_status="not_member", tag_synced=None, tag_chat_id=None, last_error=None)
                log.info("telegram.tag.skipped operator_id=%s reason=not_member", link.operator_id)
                return "skipped:not_member"
            if not member.taggable:
                await self._save(link, chat_status="admin", tag_synced=None, tag_chat_id=None, last_error=None)
                log.info("telegram.tag.skipped operator_id=%s reason=chat_admin", link.operator_id)
                return "skipped:admin"
            before = member.tag
            if member.tag == desired:
                await self._save(link, chat_status="member", tag_synced=desired, tag_chat_id=chat_id, last_error=None)
                return "unchanged"

        try:
            await self._set_tag(chat_id, link.telegram_user_id, desired)
        except TagError as err:
            return await self._on_set_error(link, err)

        await self._save(link, chat_status="member", tag_synced=desired, tag_chat_id=chat_id, last_error=None)
        log.info("telegram.tag.changed operator_id=%s telegram_user_id=%s from=%s to=%s reason=%s",
                 link.operator_id, link.telegram_user_id, tag_slug(before), tag_slug(desired), reason)
        return "changed"

    async def _member(self, chat_id: int, link: TelegramLink) -> MemberInfo | None:
        try:
            return await self.api.member(chat_id, link.telegram_user_id)
        except Exception as exc:  # noqa: BLE001 — ошибку разбираем и пишем в состояние привязки
            err = classify(exc)
            await self._record_error(link, err)
            return None

    async def _set_tag(self, chat_id: int, user_id: int, tag: str) -> None:
        try:
            await self.api.set_tag(chat_id, user_id, tag)
        except Exception as exc:
            err = classify(exc)
            if err.kind != "rate_limited" or err.retry_after > 30:
                raise err from exc
            # короткий флуд-контроль — выжидаем и пробуем ещё раз; длинный — оставляем сверке
            await asyncio.sleep(err.retry_after + 0.5)
            try:
                await self.api.set_tag(chat_id, user_id, tag)
            except Exception as exc2:
                raise classify(exc2) from exc2

    async def _on_set_error(self, link: TelegramLink, err: TagError) -> str:
        if err.kind == "not_member":
            await self._save(link, chat_status="not_member", tag_synced=None, tag_chat_id=None, last_error=None)
            return "skipped:not_member"
        if err.kind == "admin":
            await self._save(link, chat_status="admin", tag_synced=None, tag_chat_id=None, last_error=None)
            return "skipped:admin"
        if err.kind == "no_rights":
            self.invalidate_rights()  # права отобрали на лету — перепроверим при следующем обращении
        await self._record_error(link, err)
        return f"failed:{err.kind}"

    async def _record_error(self, link: TelegramLink, err: TagError) -> None:
        status = {"no_rights": "no_rights", "no_chat": "no_chat"}.get(err.kind, "error")
        # tag_synced сбрасываем: состояние в чате неизвестно — следующая сверка проверит заново
        await self._save(link, chat_status=status, tag_synced=None, last_error=err.kind)
        log.warning("telegram.tag.failed operator_id=%s telegram_user_id=%s reason=%s detail=%s",
                    link.operator_id, link.telegram_user_id, err.kind, err.detail[:200])

    # ── все сразу ──────────────────────────────────────────────────────────
    async def reconcile_all(self, reason: str, *, verify: bool = False) -> ReconcileSummary:
        summary = ReconcileSummary()
        async with self._reconcile_lock:
            for operator_id in list(self.links):
                result = await self.sync_operator(operator_id, verify=verify, reason=reason)
                summary.add(result)
                if result != "unchanged" and self.pause:
                    await asyncio.sleep(self.pause)
        log.info(
            "telegram.reconcile.completed reason=%s verify=%s total=%s changed=%s unchanged=%s skipped=%s failed=%s details=%s",
            reason, verify, summary.total, summary.changed, summary.unchanged, summary.skipped, summary.failed,
            summary.reasons or "-",
        )
        return summary

    # ── отвязки: снять тег с Telegram, который больше ни к кому не привязан ──
    async def process_unlinks(self) -> int:
        pending = await self.store.pending_unlinks()
        done = 0
        for u in pending:
            result = await self._clear_unlinked(u)
            if result is None:
                continue
            try:
                await self.store.finish_unlink(u.id, result)
                done += 1
                log.info("telegram.unlink.processed operator_id=%s telegram_user_id=%s result=%s",
                         u.operator_id, u.telegram_user_id, result)
            except Exception:
                log.exception("telegram.unlink.save_failed id=%s", u.id)
        return done

    async def _clear_unlinked(self, u: PendingUnlink) -> str | None:
        """None — оставить в очереди и попробовать позже."""
        if self.by_telegram_id(u.telegram_user_id):
            return "relinked"  # этот Telegram уже снова чей-то — тег ведёт обычная синхронизация
        stale = self._age_days(u.unlinked_at) >= UNLINK_RETRY_DAYS
        chat_id = await self.chat_id()
        if not chat_id:
            return "no_chat" if stale else None
        if await self.rights(chat_id) != RIGHTS_OK:
            return "no_rights" if stale else None
        try:
            await self._set_tag(chat_id, u.telegram_user_id, "")
        except TagError as err:
            if err.kind in ("not_member", "admin"):
                return err.kind
            log.warning("telegram.unlink.clear_failed telegram_user_id=%s reason=%s", u.telegram_user_id, err.kind)
            return err.kind if stale else None
        return "cleared"

    def _age_days(self, iso: str) -> float:
        try:
            ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (self._now() - ts).total_seconds() / 86400
