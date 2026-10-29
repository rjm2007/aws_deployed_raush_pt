# app/core/ — config and logging

Parent: `../CLAUDE.md`

- `config.py` — loads `.env`; exposes Tebra creds, `PRACTICE_ID/PROVIDER_ID/RESOURCE_ID`, Vapi assistant IDs, Twilio, Supabase headers.
  - `LOCATION_MAP`: spoken name → Tebra service location name + ID (Dana Point 7, Laguna Niguel 1, Mission Viejo 29, Fort Fitness 30). `TEBRA_VALID_NAMES` also has Laguna Hills 20.
  - `APPOINTMENT_REASON_MAP`: service words → Tebra reason ID (95 Follow up, 96 Evaluation = default, 97 "Consultaion" (Tebra's typo — keep), 98 Re-Eval, 99 Alter-G, 100 Bike Right).
  - `resolve_location()`, `resolve_appointment_reason_id()` helpers.
  - DST is automatic via zoneinfo (`CLINIC_TIMEZONE`). `CLINIC_TZ_OFFSET` / `TEBRA_TIMEZONE_OFFSET_FROM_GMT` are legacy, unused for math.
  - Visit lengths: `VISIT_TYPES` (evaluation 96 = 60 min, follow_up 95 = 45 min), `visit_duration_minutes()`, `reason_duration_minutes()`.
- `logger.py` — shared `logger`.

New locations, services or providers must be added here (until moved to a data store).
