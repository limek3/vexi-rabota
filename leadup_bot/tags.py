"""Telegram-часть тегов участников: права бота, состояние участника, установка тега.

Bot API 9.5+: ``setChatMemberTag(chat_id, user_id, tag)`` — тег обычного участника группы,
0–16 символов, без эмодзи; пустая строка снимает тег. Боту нужно быть администратором
с правом ``can_manage_tags``. Администраторам чата тег не ставится (у них custom title).
Текущий тег виден в ``getChatMember`` → ChatMemberMember/ChatMemberRestricted.tag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from telegram import Bot
from telegram.constants import ChatMemberStatus
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

log = logging.getLogger(__name__)

# Статусы прав бота в рабочем чате (пишутся в telegram_bot_state.tag_rights, их читает CRM).
RIGHTS_OK = "ok"
RIGHTS_NOT_ADMIN = "not_admin"
RIGHTS_NO_MANAGE_TAGS = "no_manage_tags"
RIGHTS_NO_CHAT = "no_chat"
RIGHTS_ERROR = "error"
RIGHTS_MISSING = (RIGHTS_NOT_ADMIN, RIGHTS_NO_MANAGE_TAGS)


class TagError(Exception):
    """Ошибка Telegram, разобранная по смыслу.

    kind: no_rights · not_member · admin · no_chat · invalid_tag · rate_limited · network · error
    """

    def __init__(self, kind: str, detail: str = "", retry_after: float = 0.0) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after


@dataclass(slots=True, frozen=True)
class MemberInfo:
    # member · restricted · admin · left · absent
    status: str
    tag: str | None = None

    @property
    def in_chat(self) -> bool:
        return self.status in ("member", "restricted", "admin")

    @property
    def taggable(self) -> bool:
        return self.status in ("member", "restricted")


class TagApi(Protocol):
    async def bot_rights(self, chat_id: int) -> str: ...
    async def member(self, chat_id: int, user_id: int) -> MemberInfo: ...
    async def set_tag(self, chat_id: int, user_id: int, tag: str) -> None: ...


_NO_RIGHTS = ("not enough rights", "chat_admin_required", "need administrator rights", "right_forbidden",
              "have no rights", "not an administrator", "can_manage_tags", "method is available only for")
_NOT_MEMBER = ("user not found", "participant_id_invalid", "user_not_participant", "member not found",
               "user is not a member", "participant not found", "user_id_invalid")
_ADMIN = ("user_admin_invalid", "administrator", "chat owner", "creator")
_NO_CHAT = ("chat not found", "bot was kicked", "bot is not a member", "group chat was upgraded", "peer_id_invalid",
            "channel_private")


def classify(exc: BaseException) -> TagError:
    if isinstance(exc, TagError):
        return exc
    if isinstance(exc, RetryAfter):
        ra = exc.retry_after
        seconds = ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)
        return TagError("rate_limited", str(exc), retry_after=seconds)
    if isinstance(exc, (TimedOut, NetworkError)) and not isinstance(exc, (BadRequest, Forbidden)):
        return TagError("network", str(exc))
    text = str(getattr(exc, "message", "") or exc).lower()
    if "tag_invalid" in text or "tag is too long" in text:
        return TagError("invalid_tag", str(exc))
    if any(s in text for s in _NO_CHAT):
        return TagError("no_chat", str(exc))
    if any(s in text for s in _NOT_MEMBER):
        return TagError("not_member", str(exc))
    if any(s in text for s in _NO_RIGHTS):
        return TagError("no_rights", str(exc))
    if isinstance(exc, Forbidden):
        return TagError("no_rights", str(exc))
    if any(s in text for s in _ADMIN):
        return TagError("admin", str(exc))
    return TagError("error", str(exc))


class TelegramTagApi:
    """Реальный Telegram через python-telegram-bot (Bot API 10.x)."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._bot_id: int | None = None

    async def _me(self) -> int:
        if self._bot_id is None:
            self._bot_id = (await self.bot.get_me()).id
        return self._bot_id

    async def bot_rights(self, chat_id: int) -> str:
        try:
            m = await self.bot.get_chat_member(chat_id=chat_id, user_id=await self._me())
        except TelegramError as exc:
            err = classify(exc)
            if err.kind in ("no_chat", "not_member"):
                return RIGHTS_NO_CHAT
            if err.kind == "no_rights":
                return RIGHTS_NOT_ADMIN
            raise err from exc
        if m.status == ChatMemberStatus.OWNER:
            return RIGHTS_OK
        if m.status != ChatMemberStatus.ADMINISTRATOR:
            return RIGHTS_NOT_ADMIN
        return RIGHTS_OK if getattr(m, "can_manage_tags", False) else RIGHTS_NO_MANAGE_TAGS

    async def member(self, chat_id: int, user_id: int) -> MemberInfo:
        try:
            m = await self.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        except TelegramError as exc:
            err = classify(exc)
            if err.kind == "not_member":
                return MemberInfo("absent")
            raise err from exc
        if m.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
            return MemberInfo("admin")
        if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            return MemberInfo("left")
        if m.status == ChatMemberStatus.RESTRICTED and not getattr(m, "is_member", True):
            return MemberInfo("left")
        status = "restricted" if m.status == ChatMemberStatus.RESTRICTED else "member"
        return MemberInfo(status, getattr(m, "tag", None) or "")

    async def set_tag(self, chat_id: int, user_id: int, tag: str) -> None:
        try:
            ok = await self.bot.set_chat_member_tag(chat_id=chat_id, user_id=user_id, tag=tag)
        except TelegramError as exc:
            raise classify(exc) from exc
        if ok is not True:
            raise TagError("error", "setChatMemberTag returned false")
