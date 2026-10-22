# app/services/ — outside systems

Parent: `../CLAUDE.md`

| File | System | Main functions |
|---|---|---|
| `tebra_service.py` | Tebra SOAP 2.1 | `call_tebra_get_appointments` (by location/day), `..._by_patient`, `..._by_patient_id`, `get_patient_by_name`, `create_patient`, `create_appointment_in_tebra`, `call_tebra_get_appointment`, `call_tebra_update_appointment`, `call_tebra_delete_appointment` |
| `supabase_service.py` | Supabase REST | insert/update `appointments`, update `leads`, upsert/fetch `inbound_calls`, `_insert_call_log`, `scheduled_callbacks`, `notification_log` |
| `twilio_service.py` | Twilio | `twilio_send_sms` (E.164 formatting) |
| `appointment_sms_service.py` | Twilio + Supabase | Builds confirmation/cancel SMS, dedupes via `notification_log` |

## Tebra rules (important)
- SOAP XML is built by hand with f-strings. **Escape** user values with `xml.sax.saxutils.escape`.
- Inside `<Fields>` and `<Filter>`, elements must be **alphabetical** or Tebra ignores them / faults.
- Check `parse_soap_fault()` and `<IsError>` on every response.
- Tebra has **no open-slots API** and **no provider hours** — availability is computed in `app/utils/time_utils.py`.
- Bookings use fixed `PRACTICE_ID`, `PROVIDER_ID`, `RESOURCE_ID` from config and a fixed 30-min length — this is what multi-provider work will change.
- Rate limits ≈ 1 request/second for GetAppointments; avoid loops of calls.
- Responses contain patient PII — don't dump full XML in chat; logs already contain raw XML.
- Full API notes: `../../../../reference/CLAUDE.md`.
