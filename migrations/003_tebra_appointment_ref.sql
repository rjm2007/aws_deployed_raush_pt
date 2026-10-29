-- 003_tebra_appointment_ref.sql
-- Tebra returns NON-numeric appointment IDs for recurring-series occurrences (e.g. '5LGN'),
-- but tebra_appointments.tebra_appointment_id is bigint. Keep the raw Tebra ID in a new
-- nullable text column; for non-numeric IDs the bigint key is a stable negative hash
-- (app/services/calendar_store.py appointment_key()). Numeric IDs are stored as-is.
-- Additive + idempotent.

alter table public.tebra_appointments add column if not exists tebra_appointment_ref text;

create index if not exists tebra_appointments_end
    on public.tebra_appointments (end_at);

notify pgrst, 'reload schema';
