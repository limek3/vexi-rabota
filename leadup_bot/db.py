from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from .models import (
    Group,
    Lead,
    MonthPlan,
    Operator,
    PendingUnlink,
    Settings,
    TelegramLink,
)

log = logging.getLogger(__name__)


class SupabaseDB:
    def __init__(self, url: str, service_key: str) -> None:
        self.base = f"{url.rstrip('/')}/rest/v1"
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), headers=self.headers)

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = await self.client.request(method, f"{self.base}/{path}", **kwargs)
                if r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"Supabase {r.status_code}: {r.text[:500]}", request=r.request, response=r)
                r.raise_for_status()
                return r
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                last = exc
                if attempt == 3:
                    raise
                await asyncio.sleep(0.75 * (2**attempt))
        assert last
        raise last

    async def fetch_all(self, table: str, *, select: str = "*", params: dict[str, str] | None = None, page: int = 1000) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            p = {"select": select, **(params or {}), "offset": str(offset), "limit": str(page)}
            r = await self._request("GET", table, params=p)
            rows = r.json()
            out.extend(rows)
            if len(rows) < page:
                return out
            offset += page

    async def healthcheck(self) -> None:
        await self._request("GET", "leads", params={"select": "id", "limit": "1"})
        await self._request("GET", "telegram_bot_state", params={"select": "key", "limit": "1"})

    async def load_leads(self) -> list[Lead]:
        rows = await self.fetch_all(
            "leads",
            select="id,at,operator_id,group_id,status,updated_at",
            params={"order": "updated_at.asc"},
        )
        return [
            Lead(
                id=str(r["id"]), at=str(r["at"]), operator_id=str(r["operator_id"]),
                group_id=(str(r["group_id"]) if r.get("group_id") is not None else None),
                status=str(r.get("status") or "work"), updated_at=str(r.get("updated_at") or ""),
            )
            for r in rows
        ]

    async def changed_leads(self, since_iso: str) -> list[Lead]:
        rows = await self.fetch_all(
            "leads",
            select="id,at,operator_id,group_id,status,updated_at",
            params={"updated_at": f"gt.{since_iso}", "order": "updated_at.asc"},
        )
        return [
            Lead(
                id=str(r["id"]), at=str(r["at"]), operator_id=str(r["operator_id"]),
                group_id=(str(r["group_id"]) if r.get("group_id") is not None else None),
                status=str(r.get("status") or "work"), updated_at=str(r.get("updated_at") or ""),
            )
            for r in rows
        ]

    async def load_reference(self) -> tuple[dict[str, Operator], dict[str, Group], dict[str, MonthPlan], Settings]:
        ops, groups, plans, kv = await asyncio.gather(
            self.fetch_all("operators", select="id,name,group_id,status,hire_date,fire_date,monthly_plan,deleted_at"),
            self.fetch_all("groups", select="id,name,monthly_plan,active,deleted_at"),
            self.fetch_all("plans", select="id,month,scope,target_id,plan"),
            self.fetch_all("kv", select="key,value", params={"key": "eq.settings"}),
        )
        op_map = {
            str(r["id"]): Operator(
                id=str(r["id"]), name=str(r.get("name") or "Без имени"),
                group_id=(str(r["group_id"]) if r.get("group_id") is not None else None),
                status=str(r.get("status") or "active"), hire_date=str(r.get("hire_date") or ""),
                fire_date=str(r.get("fire_date") or ""),
                monthly_plan=(float(r["monthly_plan"]) if r.get("monthly_plan") is not None else None),
                deleted_at=(str(r["deleted_at"]) if r.get("deleted_at") is not None else None),
            ) for r in ops
        }
        group_map = {
            str(r["id"]): Group(
                id=str(r["id"]), name=str(r.get("name") or "Без названия"),
                monthly_plan=float(r.get("monthly_plan") or 0), active=bool(r.get("active", True)),
                deleted_at=(str(r["deleted_at"]) if r.get("deleted_at") is not None else None),
            ) for r in groups
        }
        plan_map = {
            str(r["id"]): MonthPlan(
                id=str(r["id"]), month=str(r["month"]), scope=str(r["scope"]),
                target_id=(str(r["target_id"]) if r.get("target_id") is not None else None),
                plan=float(r.get("plan") or 0),
            ) for r in plans
        }
        settings_raw = kv[0].get("value") if kv else {}
        return op_map, group_map, plan_map, Settings.from_json(settings_raw if isinstance(settings_raw, dict) else {})

    async def get_state(self, key: str) -> str | None:
        r = await self._request("GET", "telegram_bot_state", params={"select": "value", "key": f"eq.{key}", "limit": "1"})
        rows = r.json()
        if not rows:
            return None
        value = rows[0].get("value")
        return str(value) if value is not None else None

    async def set_state(self, key: str, value: str) -> None:
        payload = {"key": key, "value": value, "updated_at": datetime.now(timezone.utc).isoformat()}
        await self._request(
            "POST", "telegram_bot_state", params={"on_conflict": "key"}, json=payload,
            headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
        )

    async def get_chat_id(self) -> int | None:
        value = await self.get_state("telegram_chat_id")
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    async def set_chat_id(self, chat_id: int) -> None:
        await self.set_state("telegram_chat_id", str(chat_id))

    async def claim_event(self, event_key: str, event_type: str, payload: dict[str, Any]) -> bool:
        body = {
            "event_key": event_key,
            "event_type": event_type,
            "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        r = await self.client.post(
            f"{self.base}/telegram_bot_events",
            params={"on_conflict": "event_key"},
            json=body,
            headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=representation"},
        )
        if r.status_code >= 500:
            r.raise_for_status()
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Cannot claim event: {r.status_code} {r.text[:500]}")
        rows = r.json() if r.content else []
        return bool(rows)

    async def release_event(self, event_key: str) -> None:
        try:
            await self._request("DELETE", "telegram_bot_events", params={"event_key": f"eq.{event_key}"})
        except Exception:
            log.exception("Failed to release event %s", event_key)

    # ── Привязка Telegram (supabase/migrations/20260923000001_telegram_link.sql) ──

    async def telegram_links_available(self) -> bool:
        """Есть ли таблицы привязки. Нет — миграцию ещё не выполнили: бот работает как раньше, без тегов."""
        r = await self.client.get(f"{self.base}/operator_telegram", params={"select": "operator_id", "limit": "1"})
        if r.status_code == 200:
            return True
        if r.status_code >= 500:
            r.raise_for_status()
        log.warning("telegram.links.unavailable status=%s — apply supabase/migrations/20260923000001_telegram_link.sql", r.status_code)
        return False

    async def load_links(self) -> list[TelegramLink]:
        rows = await self.fetch_all(
            "operator_telegram",
            select="operator_id,telegram_user_id,telegram_username,telegram_first_name,telegram_last_name,"
            "tag_synced,tag_chat_id,chat_status,last_error",
            params={"order": "operator_id.asc"},
        )
        return [TelegramLink.from_row(r) for r in rows]

    async def update_link_state(self, operator_id: str, telegram_user_id: int, fields: dict[str, Any]) -> None:
        """Состояние тега в строке привязки. Фильтр по telegram_user_id: если человек за это время
        отвязался или переподключил другой Telegram, чужую строку не трогаем."""
        body = {**fields, "updated_at": datetime.now(timezone.utc).isoformat()}
        await self._request(
            "PATCH", "operator_telegram",
            params={"operator_id": f"eq.{operator_id}", "telegram_user_id": f"eq.{telegram_user_id}"},
            json=body, headers={**self.headers, "Prefer": "return=minimal"},
        )

    async def consume_link_code(
        self, code: str, telegram_user_id: int, username: str | None, first_name: str | None, last_name: str | None,
    ) -> dict[str, Any]:
        r = await self._request(
            "POST", "rpc/telegram_link_consume",
            json={
                "p_code": code, "p_telegram_user_id": telegram_user_id,
                "p_username": username, "p_first_name": first_name, "p_last_name": last_name,
            },
        )
        data = r.json()
        return data if isinstance(data, dict) else {"ok": False, "reason": "bad_response"}

    async def pending_unlinks(self, limit: int = 50) -> list[PendingUnlink]:
        r = await self._request(
            "GET", "telegram_unlinks",
            params={
                "select": "id,operator_id,telegram_user_id,unlinked_at",
                "processed_at": "is.null", "order": "unlinked_at.asc", "limit": str(limit),
            },
        )
        return [
            PendingUnlink(id=int(x["id"]), operator_id=str(x["operator_id"]),
                          telegram_user_id=int(x["telegram_user_id"]), unlinked_at=str(x["unlinked_at"]))
            for x in r.json()
        ]

    async def finish_unlink(self, unlink_id: int, result: str) -> None:
        await self._request(
            "PATCH", "telegram_unlinks", params={"id": f"eq.{unlink_id}"},
            json={"processed_at": datetime.now(timezone.utc).isoformat(), "cleanup_result": result},
            headers={**self.headers, "Prefer": "return=minimal"},
        )

    async def seed_events(self, items: Iterable[tuple[str, str, dict[str, Any]]]) -> int:
        rows = [
            {
                "event_key": key,
                "event_type": typ,
                "payload": payload,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            for key, typ, payload in items
        ]
        if not rows:
            return 0
        inserted = 0
        for start in range(0, len(rows), 500):
            chunk = rows[start:start + 500]
            r = await self.client.post(
                f"{self.base}/telegram_bot_events",
                params={"on_conflict": "event_key"},
                json=chunk,
                headers={**self.headers, "Prefer": "resolution=ignore-duplicates,return=representation"},
            )
            r.raise_for_status()
            inserted += len(r.json() if r.content else [])
        return inserted
