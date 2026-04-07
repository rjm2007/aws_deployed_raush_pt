-- Inbound calls storage for CRM mirroring and idempotent event ingestion
-- Apply in Supabase SQL editor before using POST /api/v1/inbound-call-event

create table if not exists public.inbound_calls (
    id uuid primary key default gen_random_uuid(),
    call_id text not null unique,
    call_direction text not null default 'inbound',
    crm_status text not null check (crm_status in ('in_progress', 'follow_up', 'manual_follow_up', 'complete')),
    caller_phone text,
    called_number text,
    lead_id uuid,
    appointment_id uuid,
    vapi_call_id text,
    call_status text,
    started_at timestamptz,
    ended_at timestamptz,
    duration_seconds integer,
    route text,
    disposition text,
    notes text,
    practice_id uuid,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_inbound_calls_lead_id on public.inbound_calls (lead_id);
create index if not exists idx_inbound_calls_appointment_id on public.inbound_calls (appointment_id);
create index if not exists idx_inbound_calls_practice_id on public.inbound_calls (practice_id);
create index if not exists idx_inbound_calls_started_at on public.inbound_calls (started_at);

create or replace function public.set_inbound_calls_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_inbound_calls_updated_at on public.inbound_calls;
create trigger trg_inbound_calls_updated_at
before update on public.inbound_calls
for each row execute function public.set_inbound_calls_updated_at();
