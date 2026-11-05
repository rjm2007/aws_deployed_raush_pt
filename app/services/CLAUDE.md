# app/services/ — outside systems

Parent: `../CLAUDE.md`

| File | System | Main functions |
|---|---|---|
| `tebra_service.py` | Tebra SOAP 2.1 | `call_tebra_get_appointments` (by location/day), `..._by_patient`, `..._by_patient_id`, `get_patient_by_name`, `create_patient`, `create_appointment_in_tebra`, `call_tebra_get_appointment`, `call_tebra_update_appointment`, `call_tebra_delete_appointment` |
| `supabase_service.py` | Supabase REST | insert/update `appointments`, update `leads`, upsert/fetch `inbound_calls`, `_insert_call_log`, `scheduled_callbacks`, `notification_log` |
| `calendar_store.py` | Supabase REST | ONLY access to clinics, clinicians, clinician_services/hours/time_off, visit_types, tebra_appointments, tebra_sync_log |
| `tebra_sync.py` | Tebra → Supabase | `sync_window`, `sync_fast` (days 0-14), `sync_slow` (15-60), `cleanup_past`, `map_row` |
| `twilio_service.py` | Twilio | `twilio_send_sms` (E.164 formatting) |
| `appointment_sms_service.py` | Twilio + Supabase | `build_appointment_sms` (friendly date/time, clinician if `provider_name`, "To reschedule or cancel, call us at +1 (877) 283-2944." — never "reply"); `send_appointment_sms_if_needed` (send now, dedupe on `notification_log` sent, never +1555); `notify_appointment_sms` (Vapi call → `notification_log` row status `pending` + `vapi_call_id`, no text yet; direct JSON → send now); `flush_pending_for_call` (end of call: claim pending → re-fetch rows → `plan_call_notifications` final state → pending row becomes `sent`/`failed`/`skipped`); `flush_stale_pending` (safety net, run by `scheduler_leads.py` every 2 min for calls whose newest pending row is >10 min old) |
| `booking_v2_service.py` | Supabase + Tebra | v2 booking helpers: cached reference data, `open_slots_for()`, `live_provider_rows()` (live pre-booking check), `resolve_patient()` (name+phone reuse), lead/appointment REST helpers |

## Tebra rules (important)
- SOAP XML is built by hand with f-strings. **Escape** user values with `xml.sax.saxutils.escape`.
- Inside `<Fields>` and `<Filter>`, elements must be **alphabetical** or Tebra ignores them / faults.
- Check `parse_soap_fault()` and `<IsError>` on every response.
- Tebra has **no open-slots API** and **no provider hours** — availability is computed in `app/utils/time_utils.py`.
- Old endpoints book `PROVIDER_ID` + 30 min. `create_appointment_in_tebra(provider_id=, duration_minutes=, notes=)` books a clinician (ResourceId stays 1).
- `call_tebra_get_practice_appointments()` = whole practice, UTC (offset 0), throttled 1/s. Recurring occurrences have NON-numeric IDs (e.g. `5LGN`).
- GetPatients needs PracticeID + PracticeName (added to the old `get_patient_by_name` too, same results). It answers IsError=true with no message when called faster than ~1/s — `find_patients_by_name()` throttles + retries; `create_patient_v2()` escapes values and can send Gender.
- CreateAppointment `Notes` works (verified 2026-10-30: echoed back).
- Rate limits ≈ 1 request/second for GetAppointments; avoid loops of calls.
- Responses contain patient PII — don't dump full XML in chat; logs already contain raw XML.
- Full API notes: `../../../../reference/CLAUDE.md`.
