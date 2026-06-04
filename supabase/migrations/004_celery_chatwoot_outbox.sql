alter table if exists public.chat_webhook_jobs
  add column if not exists run_at timestamptz not null default now(),
  add column if not exists locked_at timestamptz,
  add column if not exists worker_id text,
  add column if not exists max_attempts integer not null default 5,
  add column if not exists completed_at timestamptz;

alter table if exists public.chat_messages
  add column if not exists processing_status text not null default 'processed',
  add column if not exists processed_at timestamptz,
  add column if not exists processing_error text;

create table if not exists public.chat_outbox_messages (
  id bigserial primary key,
  conversation_id bigint not null references public.chat_conversations(id) on delete cascade,
  external_conversation_id text not null,
  channel text not null,
  content text not null,
  status text not null default 'pending',
  idempotency_key text not null,
  attempts integer not null default 0,
  max_attempts integer not null default 5,
  error text,
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  unique (idempotency_key)
);

create unique index if not exists chat_processed_events_event_key_idx
  on public.chat_processed_events (event_key);

create unique index if not exists chat_messages_conversation_external_role_idx
  on public.chat_messages (conversation_id, external_message_id, role);

create index if not exists chat_messages_processing_idx
  on public.chat_messages (conversation_id, processing_status, created_at);

create unique index if not exists chat_webhook_jobs_event_key_idx
  on public.chat_webhook_jobs (event_key);

create index if not exists chat_webhook_jobs_status_run_at_idx
  on public.chat_webhook_jobs (status, run_at);

create index if not exists chat_webhook_jobs_locked_at_idx
  on public.chat_webhook_jobs (status, locked_at);

create index if not exists chat_outbox_messages_status_created_idx
  on public.chat_outbox_messages (status, created_at);

create unique index if not exists chat_outbox_messages_idempotency_key_idx
  on public.chat_outbox_messages (idempotency_key);

create or replace function public.increment_chat_outbox_attempts(p_outbox_id bigint)
returns void
language sql
security definer
set search_path = public
as $$
  update public.chat_outbox_messages
  set attempts = attempts + 1
  where id = p_outbox_id;
$$;

create or replace function public.increment_chat_webhook_job_attempts_for_conversation(
  p_channel text,
  p_external_conversation_id text
)
returns void
language sql
security definer
set search_path = public
as $$
  update public.chat_webhook_jobs
  set attempts = attempts + 1
  where channel = p_channel
    and external_conversation_id = p_external_conversation_id
    and status = 'processing';
$$;

create or replace function public.requeue_stale_chat_webhook_jobs(
  p_stale_minutes integer default 15,
  p_limit integer default 100
)
returns bigint[]
language plpgsql
security definer
set search_path = public
as $$
declare
  v_conversation_ids bigint[];
begin
  with stale_jobs as (
    select j.id, c.id as conversation_id
    from public.chat_webhook_jobs j
    join public.chat_conversations c
      on c.channel = j.channel
     and c.external_conversation_id = j.external_conversation_id
    where j.status = 'processing'
      and coalesce(j.locked_at, j.started_at, j.created_at) < now() - make_interval(mins => greatest(p_stale_minutes, 1))
      and j.attempts < j.max_attempts
    order by coalesce(j.locked_at, j.started_at, j.created_at)
    limit greatest(p_limit, 1)
  ),
  updated as (
    update public.chat_webhook_jobs j
    set status = 'retry',
        run_at = now(),
        locked_at = null,
        worker_id = null,
        error = null
    from stale_jobs s
    where j.id = s.id
    returning s.conversation_id
  )
  select coalesce(array_agg(distinct conversation_id), '{}'::bigint[])
  into v_conversation_ids
  from updated;

  return v_conversation_ids;
end;
$$;

create or replace function public.due_chat_webhook_job_conversations(p_limit integer default 100)
returns bigint[]
language sql
security definer
set search_path = public
as $$
  select coalesce(array_agg(distinct conversation_id), '{}'::bigint[])
  from (
    select c.id as conversation_id
    from public.chat_webhook_jobs j
    join public.chat_conversations c
      on c.channel = j.channel
     and c.external_conversation_id = j.external_conversation_id
    where j.status in ('queued', 'retry')
      and j.run_at <= now()
      and j.attempts < j.max_attempts
    order by j.run_at, j.created_at
    limit greatest(p_limit, 1)
  ) due;
$$;

create or replace function public.cleanup_expired_chat_conversation_locks()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_count integer;
begin
  update public.chat_conversations
  set locked_until = null
  where locked_until is not null
    and locked_until < now();

  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

grant select, insert, update on public.chat_outbox_messages to service_role;
grant usage, select on sequence public.chat_outbox_messages_id_seq to service_role;
grant execute on function public.increment_chat_outbox_attempts(bigint) to service_role;
grant execute on function public.increment_chat_webhook_job_attempts_for_conversation(text, text) to service_role;
grant execute on function public.requeue_stale_chat_webhook_jobs(integer, integer) to service_role;
grant execute on function public.due_chat_webhook_job_conversations(integer) to service_role;
grant execute on function public.cleanup_expired_chat_conversation_locks() to service_role;

notify pgrst, 'reload schema';
