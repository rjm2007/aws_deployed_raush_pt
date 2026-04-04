# Rausch PT Tebra API

**Base URL (production):** `https://backend.aibolt.ai`  
**Swagger UI:** `https://backend.aibolt.ai/docs`  
**Local dev:** `http://localhost:8000/docs`

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

## Clinic Hours (PDT — California)

Appointments are only bookable in 30-minute slots within:
- **7:00 AM – 2:00 PM**
- **3:00 PM – 5:30 PM**

> **Time zone reminder for India (IST):** Add **+12 hours 30 minutes** to clinic time.  
> Example: `9:00 AM PDT` → check Tebra at **9:30 PM IST same day**.

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
| `phone` | ✅ | `9491234567` | 10-digit, no dashes |
| `location` | ✅ | `Dana Point` | See locations table above |
| `service` | ❌ | `evaluation` | Defaults to evaluation |
| `lead_id` | ❌ | `uuid-here` | Supabase lead UUID — links appointment to lead |

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

Marks old appointment as Rescheduled in Tebra, creates a new one, and updates Supabase.

| Field | Required | Example | Notes |
|---|---|---|---|
| `tebra_appointment_id` | ✅ | `33463` | Old Tebra appointment ID |
| `appointment_id` | ✅ | `uuid-here` | Supabase UUID of the old appointment |
| `new_date` | ✅ | `2026-04-16` | YYYY-MM-DD |
| `new_time` | ✅ | `10:00` | HH:MM 24-hr |
| `location` | ❌ | `Dana Point` | Defaults to same location as original |
| `service` | ❌ | `follow up` | Defaults to same service as original |
| `lead_id` | ❌ | `uuid-here` | Supabase lead UUID |

> If the new time slot is also taken, it rolls back and returns nearest alternatives.

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
