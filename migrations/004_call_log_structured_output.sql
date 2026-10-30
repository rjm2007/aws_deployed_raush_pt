-- 004_call_log_structured_output.sql
-- Store the Vapi structured output "rausch_outbound_call_outcome" (post-call backup) on each call log.
-- Additive + idempotent.

alter table public.call_logs add column if not exists structured_output jsonb;

notify pgrst, 'reload schema';
