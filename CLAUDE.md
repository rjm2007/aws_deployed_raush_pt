# aws_deployed_raush_pt/ — Rausch PT backend (deployed)

Parent: `../../CLAUDE.md` · Children: `app/CLAUDE.md` (and its subfolders)

FastAPI backend + background schedulers for the Rausch PT voice/SMS agents. Deployed on AWS EC2 with Docker Compose behind Caddy at `https://backend.aibolt.ai` (Swagger: `/docs`). All routes are under `/api/v1`.

## Moving parts
| Piece | File | Runs as |
|---|---|---|
| API (Vapi tool endpoints + webhook) | `app/main.py` → `app/api/*` | container `api` (uvicorn :8000) |
| Outbound lead caller | `scheduler_leads.py` | container `scheduler-leads` |
| Appointment reminders (24hr / 2hr) | `scheduler_reminders.py` | container `scheduler-reminders` |
| New-lead intro SMS (optional) | `scheduler_leads_sms.py` | container `scheduler-leads-sms` |
| HTTPS reverse proxy | `Caddyfile` | container `caddy` |
| `main.py` (root) | Duplicate of `app/main.py`; Docker runs `app.main:app` | — |

## External systems
- **Vapi** — voice agents. Persona **Elliot** (Vapi voice `Elliot`, model `gpt-5.6-terra`, reasoning low, fast tier) on both outbound lead `a4fef714-…` and inbound (ID in `.env`); reminder `cdee681d-…`. Clinic FAQ is inlined in the prompts (no knowledge-base tool). Prompt sources + backups of older configs: `../../backups/vapi/`. GPT-Live test copies (not on any number): outbound `091ef3c2-…`, inbound `c27d440a-…`. Tools are Vapi `apiRequest`/function tools pointing at this API. Vapi MCP in `.mcp.json`.
- **Tebra (Kareo) SOAP 2.1** — patients and appointments. See `app/services/CLAUDE.md`.
- **Supabase** (project `aquzwrxukgukrpswjmdj`) — tables: `leads`, `appointments`, `inbound_calls`, `call_logs`, `notification_log`, `scheduled_callbacks`, `sms_conversations`. Supabase MCP in `.mcp.json`.
- **Twilio** — outbound SMS only (intro, confirmations, reminders). No inbound SMS handling in this repo (an n8n SMS agent handled replies).

## Call flows
1. **Outbound lead:** `scheduler_leads.py` polls `leads` → marks `in_progress` → starts Vapi call with variables `lead_id, patient_name, patient_phone, service, location, today_date` → agent calls `check-availability`, `create-appointment`, `update-lead-status` → `vapi-webhook` writes `call_logs` at end of call.
2. **Reminders:** `scheduler_reminders.py` (cron :05 and :35) sends **Twilio SMS** for tomorrow (24hr) / today (2hr), sets `reminder_sent_*` flags. (README still describes Vapi reminder calls — code now uses SMS.)
3. **Inbound:** caller → Vapi inbound assistant → `inbound-caller-lookup`, `check-availability`, `create-appointment`, `inbound-lookup-appointments`, `reschedule-appointment`, `confirm-appointment`, `update-inbound-status`.

## Conventions (follow these)
- Every POST endpoint accepts **two formats**: direct JSON (Swagger) and the Vapi tool-call wrapper `{"message":{"toolCalls":[{"id","function":{"arguments"}}]}}`. Responses go through `build_vapi_response()` → `{"results":[{"toolCallId","result"}]}` and must be **single-line text**.
- Time zone is **America/Los_Angeles**; Tebra start/end times are sent in UTC.
- Booking endpoints are **idempotent** (Vapi retries on network errors) — keep the idempotency checks when editing.
- Each request gets a short `rid` used in every log line; keep that pattern.
- Cancel and reschedule use Tebra **DeleteAppointment** (frees the slot); reschedule = delete old + create new.

## Known limits / gotchas
- **Single calendar:** every booking uses one `PRACTICE_ID/PROVIDER_ID/RESOURCE_ID` from `.env` (all `1`). Availability treats any booking at a location as blocking. Multi-provider is the next big change.
- Appointments are fixed **30 min**; clinic hours live in `app/utils/time_utils.py` and are repeated in the Vapi prompts — keep both in sync.
- `scheduler-reminders` container was found NOT running (Oct 13, 2026) — reminder SMS are not being sent until it's started.
- Outbound calls get variables `patient_first_name` (spoken) and `booking_name` (raw, for Tebra).
- `CLINIC_TZ_OFFSET` / `TEBRA_TIMEZONE_OFFSET_FROM_GMT` are **manual DST** values (-7 now, change to -8 in November).
- `.env` currently has `LEADS_TEST_POLL_SECONDS` set → outbound scheduler runs in **test mode** (polls every N seconds and skips the office-hours check).

## Running
- Local: `uvicorn app.main:app --reload --port 8000` (needs `.env`).
- Deploy: push → SSH (`../../keys/tebra.pem`) → `cd ~/project && git pull && docker compose up -d --build api scheduler-leads scheduler-reminders`. Full steps: `../../Deployment_steps.md`.
- Logs: `docker logs -f api` or `logs/*.log` on the server.
- No automated tests in this repo.

## Secrets
`.env` (all credentials) and `.mcp.json` (Vapi key) are gitignored — never print or commit them.
