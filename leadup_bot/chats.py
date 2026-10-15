"""Рабочие чаты: чаты команд (групп LEADUP) и общий чат отдела.

Каждую команду можно подключить к своему Telegram-чату (`/connect СЕКРЕТ` в этом чате → выбрать
группу). Тогда всё про оператора — грейды, рекорды, план дня и месяца, тег «Грейд I–IV» — уходит
в чат его группы, события группы — туда же, а события всего отдела — во все подключённые чаты.

Общий чат отдела — как раньше (`/connect СЕКРЕТ` без групп): туда идут события групп, у которых
своего чата нет, и итоги дня по всему отделу. Один чат подключается только к одному месту.

Хранится в telegram_bot_state: `telegram_chat_id` — общий чат, `telegram_chat_id:group:<id>` — чат группы.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAIN_KEY = "telegram_chat_id"
GROUP_PREFIX = "telegram_chat_id:group:"

# Куда отправить событие: («group», id группы) — чат группы (или общий, если своего нет);
# («team», None) — все подключённые чаты; («chat», id чата) — ровно этот чат.
Target = tuple[str, str | None]
TEAM: Target = ("team", None)


def group_target(group_id: str | None) -> Target:
    return ("group", group_id)


def chat_target(chat_id: int) -> Target:
    return ("chat", str(chat_id))


@dataclass(frozen=True, slots=True)
class Chats:
    main: int | None = None
    groups: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_state(cls, rows: dict[str, str]) -> Chats:
        main: int | None = None
        groups: dict[str, int] = {}
        for key, value in rows.items():
            try:
                chat_id = int(str(value).strip())
            except ValueError:
                continue
            if key == MAIN_KEY:
                main = chat_id
            elif key.startswith(GROUP_PREFIX) and key[len(GROUP_PREFIX):]:
                groups[key[len(GROUP_PREFIX):]] = chat_id
        return cls(main, groups)

    def all(self) -> list[int]:
        out: list[int] = []
        for c in (self.main, *self.groups.values()):
            if c and c not in out:
                out.append(c)
        return out

    def any(self) -> bool:
        return bool(self.all())

    def for_group(self, group_id: str | None) -> int | None:
        """Чат группы; своего нет — общий чат отдела; нет и его — None (событие не публикуется)."""
        if group_id and group_id in self.groups:
            return self.groups[group_id]
        return self.main

    def group_of(self, chat_id: int) -> str | None:
        return next((gid for gid, c in self.groups.items() if c == chat_id), None)

    def resolve(self, target: Target) -> list[int]:
        kind, value = target
        if kind == "team":
            return self.all()
        if kind == "chat":
            return [int(value)] if value else []
        chat = self.for_group(value)
        return [chat] if chat else []
