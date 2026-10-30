# migrations/

SQL for the Supabase schema used by multi-provider booking. Additive only: no drops, renames or truncates.
Every file is idempotent, so running it twice is safe.

| File | What it does |
|---|---|
| `001_multi_provider.sql` | Creates the new tables, adds 5 nullable columns to `appointments`, grants anon/authenticated access (RLS off like the other app tables), reloads the PostgREST schema |
| `002_seed_multi_provider.sql` | Upserts clinics, visit types, the 22 clinicians, clinician services and weekly hours |
| `003_tebra_appointment_ref.sql` | Adds nullable `tebra_appointments.tebra_appointment_ref` (raw Tebra ID; recurring occurrences have non-numeric IDs like `5LGN`) + index on `end_at` |
| `004_call_log_structured_output.sql` | Adds nullable `call_logs.structured_output` jsonb (Vapi structured output `rausch_outbound_call_outcome`, read by `/vapi-webhook`) |

## How to apply
DDL needs the Postgres connection string `SUPABASE_DB_URL` from the backend `.env` (the anon REST key cannot run DDL).
From `backend/aws_deployed_raush_pt/`:

```bash
uv run --python 3.11 --with python-dotenv --with 'psycopg[binary]' python - <<'PY'
import psycopg
from dotenv import dotenv_values
url = dotenv_values('.env')['SUPABASE_DB_URL']
with psycopg.connect(url, autocommit=True) as c:
    for f in ['migrations/001_multi_provider.sql', 'migrations/002_seed_multi_provider.sql', 'migrations/003_tebra_appointment_ref.sql', 'migrations/004_call_log_structured_output.sql']:
        c.execute(open(f).read()); print('applied', f)
PY
```
Or paste each file into the Supabase SQL editor, in order.

## Verify
Via REST with the anon key: `clinics` = 4 rows, `clinicians` = 22, `visit_types` = 2, `clinician_services` = 23, `clinician_hours` = 115.

## Tables
| Table | Purpose |
|---|---|
| `clinics` | Tebra service locations: 1 Laguna Niguel, 7 Dana Point, 29 Mission Viejo, 30 Fort Fitness. `lead_location_label` = the value in `leads.preferred_location` |
| `clinicians` | Tebra providers. `gender` is female/male/unknown, taken only from website bio pronouns (`gender_source`); never from a first name. `bookable=false` = AI never books them (1 Kevin Rausch, 4387 Kaitlynne Forbes) |
| `visit_types` | `evaluation` = reason 96, 60 min ; `follow_up` = reason 95, 45 min |
| `clinician_services` | Who offers which service line where: `physical_therapy` (all bookable clinicians at their clinic) or `pelvic_health` (website-flagged only: 4138 Danielle Simons @ LN, 5955 Analise Richtsmeier @ MV) |
| `clinician_hours` | Weekly working hours, clinic-local time, weekday 0=Mon..6=Sun. Several rows per day = split shift. Seeded from clinic opening hours + 3 DEMO split shifts |
| `clinician_time_off` | One-off exceptions per date: `off` (whole day if times are null, else that window) or `extra` (extra hours; set `tebra_location_id`) |
| `tebra_appointments` | Supabase copy of the Tebra calendar, keyed by Tebra appointment ID. `source` = `sync` (sync job) or `ai_booking` (inserted right after an AI booking). `start_at`/`end_at` are timestamptz (UTC); `appt_type` P=patient, O=blocked time |
| `tebra_sync_log` | One row per sync run: window, row count, ok/error |
| `appointments` (new cols) | `tebra_provider_id`, `provider_name`, `visit_type`, `duration_minutes`, `end_time` (all nullable) |

## Seed caveats
- Weekly hours are **clinic opening hours**, not real rosters. Replace with the client's schedules when we get them.
- DEMO split shifts (made up, for testing): Sarah Young Mon 7-11 + 14-19 ; Joelle Shoemaker Tue 7-12 Mission Viejo + 13-17 Fort Fitness ; Clay Simons Fri 7-12.
- Joelle Shoemaker is split between clinics with no overlap: MV Mon + Wed 7-17, FF Thu 8-17 + Fri 8-13, Tue split (above).
- Fort Fitness Saturday (8-13 in `get_location_hours`) is not seeded for any clinician, so Fort Fitness has no Saturday slots.
