-- 001_multi_provider.sql
-- Multi-provider booking: clinic / clinician / hours tables + a Supabase copy of the Tebra calendar.
-- Additive only and idempotent (safe to run again). Nothing is dropped or renamed.
-- Apply: see migrations/README.md

begin;

-- ─── Clinics (Tebra service locations) ───────────────────────────────────────
create table if not exists public.clinics (
    tebra_location_id    int primary key,
    name                 text not null,
    tebra_location_name  text,
    lead_location_label  text,          -- matches leads.preferred_location / LOCATION_MAP keys
    active               boolean not null default true
);

-- ─── Clinicians (Tebra providers) ────────────────────────────────────────────
create table if not exists public.clinicians (
    tebra_provider_id  int primary key,
    full_name          text not null,
    degree             text,
    gender             text not null default 'unknown'
                       check (gender in ('female', 'male', 'unknown')),
    gender_source      text,            -- where the gender came from (bio URL / pronoun); never a first-name guess
    specialties        text[] not null default '{}',
    bookable           boolean not null default true,   -- false = AI must never book this clinician
    active             boolean not null default true,
    notes              text
);

-- ─── Visit types (Tebra appointment reason + length) ─────────────────────────
create table if not exists public.visit_types (
    code              text primary key,  -- 'evaluation' | 'follow_up'
    tebra_reason_id   int not null,
    duration_minutes  int not null check (duration_minutes > 0),
    label             text
);

-- ─── Which clinician offers which service line at which clinic ───────────────
create table if not exists public.clinician_services (
    tebra_provider_id  int  not null references public.clinicians(tebra_provider_id),
    tebra_location_id  int  not null references public.clinics(tebra_location_id),
    service_line       text not null check (service_line in ('physical_therapy', 'pelvic_health')),
    primary key (tebra_provider_id, tebra_location_id, service_line)
);

-- ─── Weekly working hours (several rows per day allowed = split shifts) ──────
create table if not exists public.clinician_hours (
    id                 bigserial primary key,
    tebra_provider_id  int  not null references public.clinicians(tebra_provider_id),
    tebra_location_id  int  not null references public.clinics(tebra_location_id),
    weekday            int  not null check (weekday between 0 and 6),   -- 0=Mon .. 6=Sun
    start_time         time not null,
    end_time           time not null,
    check (end_time > start_time)
);
create unique index if not exists clinician_hours_uq
    on public.clinician_hours (tebra_provider_id, tebra_location_id, weekday, start_time);

-- ─── One-off exceptions: day off / partial off, or extra hours ───────────────
create table if not exists public.clinician_time_off (
    id                 bigserial primary key,
    tebra_provider_id  int  not null references public.clinicians(tebra_provider_id),
    date               date not null,
    start_time         time,            -- null + null = whole day
    end_time           time,
    kind               text not null default 'off' check (kind in ('off', 'extra')),
    tebra_location_id  int references public.clinics(tebra_location_id),  -- needed for 'extra'
    reason             text
);
create index if not exists clinician_time_off_provider_date
    on public.clinician_time_off (tebra_provider_id, date);

-- ─── Supabase copy of the Tebra calendar (filled by the sync job + AI bookings) ──
create table if not exists public.tebra_appointments (
    tebra_appointment_id  bigint primary key,
    tebra_provider_id     int,          -- null = no clinician resource on the appt
    tebra_location_id     int,
    start_at              timestamptz not null,
    end_at                timestamptz not null,
    duration_minutes      int,
    status                text,         -- Tebra ConfirmationStatus / AppointmentStatus
    appt_type             text,         -- P = patient, O = other / blocked time
    tebra_patient_id      bigint,
    source                text not null default 'sync' check (source in ('sync', 'ai_booking')),
    synced_at             timestamptz not null default now()
);
create index if not exists tebra_appointments_provider_start
    on public.tebra_appointments (tebra_provider_id, start_at);
create index if not exists tebra_appointments_start
    on public.tebra_appointments (start_at);

-- ─── Sync job runs ───────────────────────────────────────────────────────────
create table if not exists public.tebra_sync_log (
    id            bigserial primary key,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    window_start  date,
    window_end    date,
    rows          int,
    ok            boolean,
    error         text
);

-- ─── New NULLABLE columns on appointments ────────────────────────────────────
alter table public.appointments add column if not exists tebra_provider_id int;
alter table public.appointments add column if not exists provider_name     text;
alter table public.appointments add column if not exists visit_type        text;
alter table public.appointments add column if not exists duration_minutes  int;
alter table public.appointments add column if not exists end_time          time;

-- ─── Access: app uses the anon REST key, RLS off like the other app tables ───
alter table public.clinics            disable row level security;
alter table public.clinicians         disable row level security;
alter table public.visit_types        disable row level security;
alter table public.clinician_services disable row level security;
alter table public.clinician_hours    disable row level security;
alter table public.clinician_time_off disable row level security;
alter table public.tebra_appointments disable row level security;
alter table public.tebra_sync_log     disable row level security;

grant select, insert, update, delete on
    public.clinics, public.clinicians, public.visit_types, public.clinician_services,
    public.clinician_hours, public.clinician_time_off, public.tebra_appointments, public.tebra_sync_log
    to anon, authenticated, service_role;

grant usage, select on
    sequence public.clinician_hours_id_seq, public.clinician_time_off_id_seq, public.tebra_sync_log_id_seq
    to anon, authenticated, service_role;

commit;

-- Tell PostgREST to pick up the new tables right away
notify pgrst, 'reload schema';
