-- Red de seguridad para mensajes del cliente que quedaron `pending` por un fallo
-- transitorio (quota de OpenAI, 5xx, red) y cuyo chat_webhook_job murió `failed`.
-- Los barridos existentes solo miran el estado del job (queued/retry/processing),
-- así que un `failed` deja los mensajes huérfanos para siempre. Esta función rescata
-- por EDAD del mensaje pending, independiente del estado del job: si el fallo era
-- transitorio, el reintento responde; si pasó el máximo, se deja de insistir (una
-- respuesta automática muy tardía no aporta).
create or replace function public.due_stranded_pending_conversations(
  p_min_age_seconds integer default 120,
  p_max_age_seconds integer default 21600,
  p_limit integer default 100
)
returns bigint[]
language sql
security definer
set search_path = public
as $$
  select coalesce(array_agg(conversation_id order by oldest_pending), '{}'::bigint[])
  from (
    select m.conversation_id, min(m.created_at) as oldest_pending
    from public.chat_messages m
    where m.role = 'user'
      and m.processing_status = 'pending'
    group by m.conversation_id
    having min(m.created_at) <= now() - make_interval(secs => greatest(p_min_age_seconds, 1))
       and min(m.created_at) >= now() - make_interval(secs => greatest(p_max_age_seconds, p_min_age_seconds + 1))
    limit greatest(p_limit, 1)
  ) due;
$$;
