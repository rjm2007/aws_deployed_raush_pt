# app/utils/ — pure helpers

Parent: `../CLAUDE.md`

- `time_utils.py` — all slot math, LA time.
  - `get_location_hours()` — clinic hours per location, matched to the client FAQ (Oct 2026): LN Mon–Fri 7–19, Sat 8–14; DP Mon–Fri 7–19; MV Mon–Fri 7–17; DP/MV alternate Saturdays are NOT bookable (unknown which); Fort Fitness unchanged (not in FAQ); Sun closed.
  - `generate_all_slots()` (30-min), `parse_booked_slots()` (Tebra XML → booked times, skips Cancelled), `get_available_slots()`, `get_free_ranges()`, `get_nearest_available_slots()`, `is_valid_clinic_slot()`.
  - Conversions: `parse_time_to_24hr`, `format_12hr`, `to_utc_string`, `iso_utc_z_to_la_parts`, `parse_tebra_local_start_datetime`.
  - Note: booked slots are not split by provider — any booking blocks the slot for the whole location.
- `parser.py` — `parse_soap_fault`, `coerce_vapi_tool_arguments`, `vapi_response_content` / `build_vapi_response` (collapses to one line — Vapi rejects multi-line results), `extract_vapi_caller_number_from_body`.
- `names.py` — `clean_person_name()` strips test labels/numbers for what agents SAY ("Kris Jorge Test 4" → "Kris"). Keep raw names for Tebra/Supabase.
