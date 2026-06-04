create table if not exists public.chat_conversations (
  id bigserial primary key,
  channel text not null,
  external_conversation_id text not null,
  external_contact_id text,
  account_id text,
  state jsonb not null default '{}'::jsonb,
  locked_until timestamptz,
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (channel, external_conversation_id)
);

create index if not exists chat_conversations_last_seen_idx
  on public.chat_conversations (last_seen_at desc);

create table if not exists public.chat_messages (
  id bigserial primary key,
  conversation_id bigint not null references public.chat_conversations(id) on delete cascade,
  external_message_id text,
  role text not null check (role in ('user', 'assistant', 'system')),
  content text not null,
  raw_payload jsonb not null default '{}'::jsonb,
  tool_calls jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  unique (conversation_id, external_message_id, role)
);

create index if not exists chat_messages_conversation_created_idx
  on public.chat_messages (conversation_id, created_at desc);

create table if not exists public.chat_processed_events (
  event_key text primary key,
  channel text not null,
  external_conversation_id text,
  external_message_id text,
  raw_payload jsonb not null default '{}'::jsonb,
  status text not null default 'received',
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists chat_processed_events_created_idx
  on public.chat_processed_events (created_at desc);

create table if not exists public.chat_webhook_jobs (
  id bigserial primary key,
  event_key text not null references public.chat_processed_events(event_key) on delete cascade,
  channel text not null,
  external_conversation_id text not null,
  external_message_id text,
  status text not null default 'queued',
  attempts integer not null default 0,
  error text,
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);

create index if not exists chat_webhook_jobs_status_created_idx
  on public.chat_webhook_jobs (status, created_at);

create or replace function public.touch_chat_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists touch_chat_conversations_updated_at on public.chat_conversations;
create trigger touch_chat_conversations_updated_at
before update on public.chat_conversations
for each row execute function public.touch_chat_updated_at();

drop trigger if exists touch_chat_processed_events_updated_at on public.chat_processed_events;
create trigger touch_chat_processed_events_updated_at
before update on public.chat_processed_events
for each row execute function public.touch_chat_updated_at();

create or replace function public.get_or_create_chat_conversation(
  p_channel text,
  p_external_conversation_id text,
  p_external_contact_id text default null,
  p_account_id text default null
)
returns public.chat_conversations
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.chat_conversations;
begin
  insert into public.chat_conversations (
    channel,
    external_conversation_id,
    external_contact_id,
    account_id,
    last_seen_at
  )
  values (
    p_channel,
    p_external_conversation_id,
    p_external_contact_id,
    p_account_id,
    now()
  )
  on conflict (channel, external_conversation_id) do update set
    external_contact_id = coalesce(excluded.external_contact_id, public.chat_conversations.external_contact_id),
    account_id = coalesce(excluded.account_id, public.chat_conversations.account_id),
    last_seen_at = now()
  returning * into v_row;

  return v_row;
end;
$$;

create or replace function public.acquire_chat_conversation_lock(
  p_channel text,
  p_external_conversation_id text,
  p_lock_seconds integer default 60
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id bigint;
begin
  select id into v_id
  from public.chat_conversations
  where channel = p_channel
    and external_conversation_id = p_external_conversation_id
  for update;

  if v_id is null then
    return false;
  end if;

  update public.chat_conversations
  set locked_until = now() + make_interval(secs => greatest(p_lock_seconds, 1))
  where id = v_id
    and (locked_until is null or locked_until < now());

  return found;
end;
$$;

create or replace function public.release_chat_conversation_lock(
  p_channel text,
  p_external_conversation_id text
)
returns void
language sql
security definer
set search_path = public
as $$
  update public.chat_conversations
  set locked_until = null
  where channel = p_channel
    and external_conversation_id = p_external_conversation_id;
$$;

create or replace function public.mark_chat_event_received(
  p_event_key text,
  p_channel text,
  p_external_conversation_id text,
  p_external_message_id text,
  p_raw_payload jsonb
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.chat_processed_events (
    event_key,
    channel,
    external_conversation_id,
    external_message_id,
    raw_payload,
    status
  )
  values (
    p_event_key,
    p_channel,
    p_external_conversation_id,
    p_external_message_id,
    coalesce(p_raw_payload, '{}'::jsonb),
    'received'
  );

  return true;
exception when unique_violation then
  return false;
end;
$$;

create or replace function public.update_chat_event_status(
  p_event_key text,
  p_status text,
  p_error text default null
)
returns void
language sql
security definer
set search_path = public
as $$
  update public.chat_processed_events
  set status = p_status,
      error = p_error
  where event_key = p_event_key;
$$;

create or replace function public.enqueue_chat_webhook_job(
  p_event_key text,
  p_channel text,
  p_external_conversation_id text,
  p_external_message_id text,
  p_raw_payload jsonb
)
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id bigint;
begin
  insert into public.chat_webhook_jobs (
    event_key,
    channel,
    external_conversation_id,
    external_message_id,
    raw_payload,
    status
  )
  values (
    p_event_key,
    p_channel,
    p_external_conversation_id,
    p_external_message_id,
    coalesce(p_raw_payload, '{}'::jsonb),
    'queued'
  )
  returning id into v_id;

  return v_id;
end;
$$;

create or replace function public.update_chat_webhook_job_status(
  p_job_id bigint,
  p_status text,
  p_error text default null
)
returns void
language sql
security definer
set search_path = public
as $$
  update public.chat_webhook_jobs
  set status = p_status,
      error = p_error,
      attempts = case when p_status = 'processing' then attempts + 1 else attempts end,
      started_at = case when p_status = 'processing' then now() else started_at end,
      finished_at = case when p_status in ('completed', 'failed', 'skipped') then now() else finished_at end
  where id = p_job_id;
$$;

grant select, insert, update on public.chat_conversations to service_role;
grant select, insert, update on public.chat_messages to service_role;
grant select, insert, update on public.chat_processed_events to service_role;
grant select, insert, update on public.chat_webhook_jobs to service_role;
grant usage, select on sequence public.chat_conversations_id_seq to service_role;
grant usage, select on sequence public.chat_messages_id_seq to service_role;
grant usage, select on sequence public.chat_webhook_jobs_id_seq to service_role;

grant execute on function public.get_or_create_chat_conversation(text, text, text, text) to service_role;
grant execute on function public.acquire_chat_conversation_lock(text, text, integer) to service_role;
grant execute on function public.release_chat_conversation_lock(text, text) to service_role;
grant execute on function public.mark_chat_event_received(text, text, text, text, jsonb) to service_role;
grant execute on function public.update_chat_event_status(text, text, text) to service_role;
grant execute on function public.enqueue_chat_webhook_job(text, text, text, text, jsonb) to service_role;
grant execute on function public.update_chat_webhook_job_status(bigint, text, text) to service_role;

notify pgrst, 'reload schema';
