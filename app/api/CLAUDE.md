# app/api/ — HTTP endpoints (Vapi tools)

Parent: `../CLAUDE.md`

All routes are `POST /api/v1/...` unless noted. Each one is also a Vapi tool used by an assistant.

| File | Route | Used by | What it does |
|---|---|---|---|
| `availability.py` | `/check-availability` | outbound, inbound | Tebra GetAppointments for date+location → free 30-min slots, or nearest slots to a requested time |
| `appointments.py` | `/create-appointment` | outbound, inbound | Find/create Tebra patient → CreateAppointment → insert Supabase `appointments` → update lead → confirmation SMS. Idempotent. |
| | `/update-appointment-status` | staff/testing | Tebra UpdateAppointment status |
| | `/reschedule-appointment` | inbound, reminder | Delete old Tebra appt + create new; update Supabase |
| | `/confirm-appointment` | reminder, inbound | Confirm, or cancel (→ DeleteAppointment); Tebra + Supabase in parallel |
| | `/cancel-appointment` | — | Cancel in Tebra + Supabase + lead |
| | `/inbound-lookup-appointments` | inbound | List upcoming appointments (Tebra by patient, merged with Supabase IDs) or lock one |
| `leads.py` | `/update-lead-status` | outbound | Patch `leads` (queue_status, lead_outcome, notes, callback time) |
| | `/update-inbound-status` (+ alias `/inbound-call-event`) | inbound | Upsert `inbound_calls` by Vapi call_id |
| | `/inbound-caller-lookup` | inbound | Find caller by phone in `leads` + upcoming `appointments` (not Tebra) |
| | `/vapi-webhook` | Vapi server URL | End-of-call report → `call_logs` (+ `structured_output` jsonb), fallback lead update. Reads Vapi structured output `rausch_outbound_call_outcome` (test agent only so far; logic in `app/utils/call_outcome.py`): lead untouched by tools + confidence ≥ 0.7 → do_not_call / not_interested / wrong_number applied, other outcomes keep the fallback with the reason in notes; lead already updated → status never changed, only a do-not-call request (≥ 0.8) sets `metadata.do_not_call`. Never books/cancels. Summary from `analysis.summary` |
| `system.py` | `GET /health` | monitoring | `{"status":"ok"}` |
| `booking_v2.py` | `/v2/check-availability` | new booking-only outbound agent, inbound MP copy | Multi-provider options (Supabase copy only): any / male / female / named clinician, date, time, range, part of day; hand-off / transfer / closed-day replies; `[ids ...]` tag for booking |
| | `/v2/create-appointment` | new outbound agent, inbound MP copy | **Two-step:** first call returns `CONFIRM FIRST` (no Tebra call, pending kept in memory 10 min); books only on a second call with `confirmed=true` for the same lead+clinician+date+time. Validate clinician+clinic+service → engine slot check → confirm gate → LIVE Tebra check → patient (reuse on name+phone) → CreateAppointment (clinician, 60/45 min) → cache + `appointments` + lead → SMS (not to +1555). Idempotent (lead+clinician+start ±10 min) |
| | `/v2/cancel-appointment` | tests / cleanup | DeleteAppointment + cache row + `appointments` cancelled. Idempotent |
| | `/v2/update-lead` | new outbound agent | do_not_call (metadata.do_not_call) / not_interested / front_desk; never downgrades a booked lead |
| `booking_v2_inbound.py` | `/v2/inbound-lookup-appointments` | inbound MP copy | GetPatients by name (caller-phone patients first, max 3) → upcoming visits; each says day, time, **clinician**, clinic, visit type + `[ids (1) tebra_appointment_id=… clinician_id=… location=… visit_type=… date=… time=… appointment_id=…]`. Clinician from calendar copy, else LIVE practice rows (never GetAppointment ProviderId) |
| | `/v2/reschedule-appointment` | inbound MP copy | **Two-step** (`CONFIRM FIRST` → `confirmed=true`, keyed old id + clinician + clinic + visit type + date + time). Owner check (our row has the Vapi caller-ID phone — a spoken `phone` arg never counts on a Vapi call — or Tebra patient has the given name). Only upcoming visits (not started) can be moved. Default = same clinician + clinic + visit type. Engine check ignores the old visit → LIVE check → **create new FIRST, then delete old** → cache + `appointments` (old → rescheduled, new `rescheduled_from_id`) → SMS (not +1555). Idempotent (memory + Supabase child row); once a visit was moved, a request for another time gives `ALREADY MOVED:` and never books a second visit. Errors after the new visit exists still answer `RESCHEDULED` (old not removed → "front desk will clear it") |
| | `/v2/inbound-cancel-appointment` | inbound MP copy | **Two-step** confirm, owner check, upcoming visits only, DeleteAppointment (a failed delete counts as gone only if the practice day list no longer has it) + cache + `appointments` cancelled + SMS. Idempotent. (`/v2/cancel-appointment` stays the no-check cleanup route) |

v2 inbound notes: `/v2/check-availability` and `/v2/create-appointment` work without `lead_id`. With no lead and a non-outbound call, create takes the phone from Vapi caller ID (confirm gate + idempotency keyed by that phone, no lead updates) and check-availability points hand-offs at `update_inbound_status` (manual_follow_up) instead of `update_lead` / transfer. With a lead the replies are unchanged.

## Patterns to keep
- Parse both formats: `if "message" in body and "toolCalls" in body["message"]` → Vapi; else direct JSON. Use `coerce_vapi_tool_arguments` when arguments may be a string.
- Return with `build_vapi_response(tool_call_id, message)` — one line of plain English the agent can read aloud.
- Inbound calls have no `lead_id`; phone comes from Vapi caller ID (`extract_vapi_caller_number_from_body`), not the tool argument.
- Swagger schemas come from `app/models/requests.py` via `openapi_extra` + `inline_schema_refs`.
- `appointments.py` is large (~1400 lines); find the function by route name before editing.
