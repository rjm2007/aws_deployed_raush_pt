# app/ — FastAPI application

Parent: `../CLAUDE.md` · Children: `api/`, `services/`, `core/`, `utils/`, `models/` (each has a CLAUDE.md)

`main.py` builds the FastAPI app and mounts 4 routers under `/api/v1`: availability, appointments, leads, system.

## Layering (keep it this way)
```
api/       → HTTP endpoints. Parse request (JSON or Vapi wrapper), orchestrate, return Vapi-style response.
services/  → Talk to outside systems: Tebra SOAP, Supabase REST, Twilio SMS.
core/      → Config (env vars, location map, reason IDs) and logger.
utils/     → Pure helpers: time/slot math, Vapi parsing, SOAP fault parsing.
models/    → Pydantic models used only for Swagger docs.
```
- Endpoints call services; services never import from `api/`.
- All outside calls use `httpx.AsyncClient` (async everywhere).
- Logging via `app.core.logger.logger`, prefixed with the request `rid`.
