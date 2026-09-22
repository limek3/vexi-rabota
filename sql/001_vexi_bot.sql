-- LEADUP / Vexi Telegram Bot
-- Run once in Supabase -> SQL Editor.
-- These are server-only tables. The browser app does not need access to them.

create table if not exists public.telegram_bot_events (
  event_key  text primary key,
  event_type text not null,
  payload    jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists telegram_bot_events_type_idx
  on public.telegram_bot_events (event_type, created_at desc);

create table if not exists public.telegram_bot_state (
  key        text primary key,
  value      text not null default '',
  updated_at timestamptz not null default now()
);

alter table public.telegram_bot_events enable row level security;
alter table public.telegram_bot_state enable row level security;

-- Never expose the bot journal/config to normal frontend users.
revoke all on table public.telegram_bot_events from anon, authenticated;
revoke all on table public.telegram_bot_state from anon, authenticated;

-- The Python service uses the Supabase Service Role key and bypasses RLS.
grant all on table public.telegram_bot_events to service_role;
grant all on table public.telegram_bot_state to service_role;
