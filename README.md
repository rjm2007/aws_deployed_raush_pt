# Rausch PT Tebra API

**Base URL (production):** `https://backend.aibolt.ai`  
**Swagger UI:** `https://backend.aibolt.ai/docs`  
**Local dev:** `http://localhost:8000/docs`

---

## Call flows (who calls what)

### Outbound — new lead (lead agent)

1. A lead exists in Supabase (`leads`, typically `queue_status=new` or retry states).
2. **`scheduler_leads.py`** (Docker service or host) polls Supabase on a schedule, marks a row `in_progress`, and starts a **Vapi** outbound call using the lead assistant.
3. The assistant uses tools on this API: **`check_availability`**, **`create_appointment`**, **`update-lead-status`**.
4. **`create-appointment`** creates/updates the patient in **Tebra**, books the slot, inserts **`appointments`**, and may send SMS (via **`notification_log`** deduping).
5. **`update-lead-status`** patches **`leads`** (`queue_status`, `lead_outcome`, notes, etc.).
6. **`vapi-webhook`** receives end-of-call events and writes **`call_logs`** (and related bookkeeping).

### Outbound — appointment reminder (24hr / 2hr)

1. **`scheduler_reminders.py`** (Docker) queries **`appointments`** for upcoming visits with `reminder_sent_24hr` / `reminder_sent_2hr` still false (plus date/window rules in LA time).
2. It places a **Vapi** reminder call with variables (appointment id, Tebra id, date, time, `reminder_type`, etc.).
3. The reminder assistant uses **`confirm-appointment`** (confirm or cancel) and, for reschedules, **`check-availability`** + **`reschedule-appointment`**.
4. Successful runs set the corresponding reminder flag on **`appointments`**; SMS paths may log to **`notification_log`**.

### Inbound (scheduling assistant)

1. Caller reaches the **Vapi** inbound assistant (no lead row required).
2. Tools: **`check-availability`**, **`create-appointment`**, **`inbound-lookup-appointments`**, **`reschedule-appointment`**, **`update-inbound-status`**.
3. **`update-inbound-status`** upserts **`inbound_calls`** keyed by Vapi **`call_id`** (`crm_status`, summary/notes, optional `appointment_id`, `route`, etc.).
4. Booking/reschedule updates **Tebra** and **`appointments`** the same way as outbound; optional webhook path can still persist inbound metadata.

### Local database reference

Create **`database_schema.md`** in this `aws/` folder if you want a concise, code-oriented view of Supabase tables (see **`.gitignore`** — that filename is **not committed**). Draft it from the Supabase SQL editor / dashboard or keep a private copy outside git.

---

## Clinic Locations

Use these **exact strings** in the `location` field of any endpoint:

| Display Name | Tebra Internal Name |
|---|---|
| `Dana Point` | Rausch Dana Point |
| `Laguna Niguel` | Rausch Physical Therapy, Inc |
| `Mission Viejo` | Rausch PT - Mission Viejo |
| `Fort Fitness - Laguna Hills` | Rausch Physical Therapy - Fort F |

---

## Services / Appointment Types

Use these strings in the `service` field:

| Value to pass | Tebra Reason ID | Notes |
|---|---|---|
| `evaluation` | 96 | Default if omitted |
| `follow up` | 95 | Also accepts `follow-up` |
| `consultation` | 97 | Also accepts `consultaion` |
| `re-eval` | 98 | Also accepts `re-evaluation`, `re eval` |
| `alter-g` | 99 | Also accepts `alter g` |
| `bike right` | 100 | |
| `pelvic health` | 96 | Same as evaluation |
| `pt` | 96 | Same as evaluation |

> If `service` is omitted or unrecognised, it defaults to **Evaluation (ID 96)**.

---

## Clinic hours (summary — America/Los_Angeles)

Slots are **30 minutes** where the schedule allows; exact open windows are enforced in **`check-availability`** (Tebra + `time_utils`). By location:

- **Laguna Niguel / Dana Point:** Mon–Fri 7:00 AM–7:00 PM, Sat 7:00 AM–1:30 PM, Sun closed  
- **Mission Viejo:** Mon–Fri 7:00 AM–5:00 PM, Sat–Sun closed  
- **Fort Fitness - Laguna Hills:** Mon–Thu 8:00 AM–5:00 PM, Fri–Sat 8:00 AM–1:00 PM, Sun closed  

> **IST note:** US Pacific vs India is roughly **+12:30** (DST shifts twice a year).

---

## Endpoints

All routes are prefixed with `/api/v1`.

---

### `GET /api/v1/health`

Health check. No parameters needed.

**Expected response:**
```json
{ "status": "ok", "service": "Rausch PT Tebra API" }
```

---

### `POST /api/v1/check-availability`

Checks Tebra live for open slots on a given date and location.

| Field | Required | Example | Notes |
|---|---|---|---|
| `date` | ✅ | `2026-04-15` | YYYY-MM-DD format |
| `location` | ✅ | `Dana Point` | See locations table above |
| `time` | ❌ | `09:00` | HH:MM 24-hr. **Omit to get ALL available slots for the day** |
| `service` | ❌ | `evaluation` | Not used for filtering — informational only |

**Test scenarios:**

1. **Get all slots for a day** — leave `time` empty, fill `date` + `location`
2. **Check a specific time** — fill all three: `date`, `location`, `time`
3. **Slot is free** → response says "Yes, 9:00 AM is available…"
4. **Slot is taken** → response gives nearest alternatives
5. **No slots at all** → response says no available slots

---

### `POST /api/v1/create-appointment`

Creates a patient (if not found) and books an appointment in Tebra + Supabase.

| Field | Required | Example | Notes |
|---|---|---|---|
| `date` | ✅ | `2026-04-15` | YYYY-MM-DD |
| `time` | ✅ | `09:00` | HH:MM or natural (`9 AM`, `1:30 PM`) |
| `name` | ✅ | `John Smith` | Patient full name |
| `phone` | ⚠️ | `+19491234567` | **Outbound (with `lead_id`):** pass patient phone. **Inbound (no `lead_id`):** omit — API uses **Vapi caller ID** from the tool request payload for Tebra / Supabase / SMS. |
| `location` | ✅ | `Dana Point` | See locations table above |
| `service` | ❌ | `evaluation` | Defaults to evaluation |
| `lead_id` | ❌ | `uuid-here` | Supabase lead UUID — links appointment to lead (outbound). Inbound bookings omit this. |

> **Vapi inbound tool:** Make `phone` **optional** in the tool schema so the model does not collect it; the server still requires a resolvable number (from payload or `phone` for tests).

**Inbound `apiRequest` and caller ID:** For tools of type **`apiRequest`**, Vapi often sends only the LLM-filled body (e.g. `date`, `time`, `name`, `location`, `service`) with **no** `message` / `call` object — so the API cannot read caller ID unless you merge it in. On the **`create_appointment`** tool in the Vapi dashboard, add **static parameters** so the customer number is always included, for example: `{ "key": "phone", "value": "{{ customer.number }}" }`. Those values merge into the POST body and override an empty model-supplied `phone`. See Vapi docs: [Static variables and aliases](https://docs.vapi.ai/tools/static-variables-and-aliases).

**Test scenarios:**

1. **New patient** — use a name that doesn't exist in Tebra; it auto-creates the patient record
2. **Existing patient** — use a name already in Tebra; it reuses the existing patient
3. **Slot conflict** — book a time that's already taken; it should reject and offer alternatives
4. **Missing field** — omit `name` or `phone`; should return a clear error

---

### `POST /api/v1/update-appointment-status`

Updates the status of an existing appointment in Tebra (and optionally Supabase).

| Field | Required | Example | Notes |
|---|---|---|---|
| `tebra_appointment_id` | ✅ | `33463` | Integer ID from Tebra |
| `new_status` | ✅ | `Confirmed` | See status values below |
| `appointment_id` | ❌ | `uuid-here` | Supabase UUID — also updates local DB if provided |

**Valid `new_status` values:**

`Confirmed` · `Cancelled` · `NoShow` · `Rescheduled` · `Scheduled` · `CheckedIn` · `CheckedOut` · `NeedsReschedule`

---

### `POST /api/v1/reschedule-appointment`

**Deletes** the old visit in Tebra (`DeleteAppointment`), **creates** a new visit at the new time, then updates Supabase (old row → `rescheduled`, new row with `rescheduled_from_id`). Idempotent when Supabase already has the new row for the same old `appointment_id` + new date/time.

| Field | Required | Example | Notes |
|---|---|---|---|
| `tebra_appointment_id` | ✅ | `33463` | Old Tebra appointment ID |
| `appointment_id` | ❌ | `uuid-here` | Supabase UUID of the old row when it exists (omit for Tebra-only visits) |
| `new_date` | ✅ | `2026-04-16` | YYYY-MM-DD |
| `new_time` | ✅ | `10:00` | HH:MM 24-hr |
| `location` | ❌ | `Dana Point` | Defaults to same location as original |
| `service` | ❌ | `follow up` | Defaults to same service as original |
| `lead_id` | ❌ | `uuid-here` | Supabase lead UUID |

> New slot is checked **before** delete. If **create** fails after delete, there is **no** Tebra rollback (retries rely on Supabase idempotency + staff follow-up). If the new slot is taken before delete, the API returns alternatives without deleting.

---

### `POST /api/v1/confirm-appointment`

Merged confirm/cancel endpoint for the Reminder Agent — runs Tebra + Supabase updates in parallel.

| Field | Required | Example | Notes |
|---|---|---|---|
| `tebra_appointment_id` | ✅ | `33463` | Tebra appointment ID |
| `appointment_id` | ✅ | `uuid-here` | Supabase appointment UUID |
| `outcome` | ✅ | `confirmed` | `confirmed` or `cancelled` |
| `lead_id` | ❌ | `uuid-here` | If provided, also updates lead outcome |
| `notes` | ❌ | `Patient confirmed.` | Call summary stored in Supabase |
| `reminder_type` | ❌ | `24hr` | `24hr` (default) or `2hr` |

---

### `POST /api/v1/update-lead-status`

Updates a lead record in Supabase after an outbound VAPI call.

| Field | Required | Example | Notes |
|---|---|---|---|
| `lead_id` | ✅ | `uuid-here` | Supabase lead UUID |
| `queue_status` | ❌ | `complete` | See queue status values below |
| `lead_outcome` | ❌ | `booked` | See outcome values below |
| `callback_requested_at` | ❌ | `2026-04-15T14:00:00Z` | ISO datetime |
| `callback_notes` | ❌ | `Call back after 2pm` | Free text |
| `tebra_patient_id` | ❌ | `12345` | Links Tebra patient to this lead |
| `notes` | ❌ | `Spoke to patient.` | Free text |

**Valid `queue_status` values:**

`new` · `in_progress` · `complete` · `not_interested` · `follow_up` · `manual_follow_up`

**Valid `lead_outcome` values:**

`booked` · `not_interested` · `no_answer` · `callback` · `manual`

---

### `POST /api/v1/vapi-webhook`

End-of-call fallback — receives VAPI server events. Not useful to test manually via Swagger. VAPI posts to this automatically after every call.

### `POST /api/v1/update-inbound-status` (inbound CRM)

Upserts **`inbound_calls`** by `call_id`. Used by the inbound assistant only.

### `POST /api/v1/inbound-lookup-appointments`

Lists upcoming visits for reschedule (Tebra + Supabase enrichment). See OpenAPI for request fields.

---

## Quick Testing Checklist

| What to test | Endpoint | Key inputs |
|---|---|---|
| Is API up? | `GET /health` | — |
| Any slots today? | `POST /check-availability` | today's date, any location, no time |
| Is 9 AM free? | `POST /check-availability` | date, location, `time: 09:00` |
| Book an appointment | `POST /create-appointment` | all required fields |
| Mark as confirmed | `POST /update-appointment-status` | tebra id, `new_status: Confirmed` |
| Reschedule | `POST /reschedule-appointment` | old tebra id, supabase id, new date+time |
| Reminder confirm/cancel | `POST /confirm-appointment` | tebra id, supabase id, outcome |
| Update lead after call | `POST /update-lead-status` | lead_id, queue_status, lead_outcome |
