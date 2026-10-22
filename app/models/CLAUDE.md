# app/models/ — Swagger request models

Parent: `../CLAUDE.md`

- `requests.py` — Pydantic models and enums (`QueueStatus`, `LeadOutcome`, `InboundCrmStatus`, `AppointmentStatus`, ...) for every endpoint.
- Used **only** for Swagger/OpenAPI docs (`openapi_extra` + `inline_schema_refs`). Endpoints parse raw JSON themselves, so changing a model does not change validation.
- When adding an endpoint field, update both the endpoint parsing and the model here.
