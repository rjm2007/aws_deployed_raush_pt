# ─────────────────────────────────────────────────────────────────────────────
# Rausch PT — FastAPI App Entry Point
#
# RUN:
#   uvicorn app.main:app --reload --port 8000
#
# (Run this command from the fastapi/ project root directory)
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI

from app.api import availability, appointments, leads, system

app = FastAPI(
    title="Rausch PT Tebra API",
    description=(
        "Backend API for Rausch Physical Therapy.\n\n"
        "Handles appointment booking, availability checks, lead management, "
        "and VAPI voice-agent tool calls.\n\n"
        "**Dual format:** Every POST endpoint accepts both direct JSON "
        "(for Swagger / testing) and the VAPI tool-call wrapper format."
    ),
    version="1.0.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
    openapi_tags=[
        {"name": "Availability", "description": "Check open appointment slots in Tebra"},
        {"name": "Appointments", "description": "Create, update, reschedule, and confirm appointments"},
        {"name": "Leads",        "description": "Lead status updates and VAPI webhook"},
        {"name": "System",      "description": "Health checks and diagnostics"},
    ],
)

app.include_router(availability.router,  prefix="/api/v1")
app.include_router(appointments.router,  prefix="/api/v1")
app.include_router(leads.router,         prefix="/api/v1")
app.include_router(system.router,        prefix="/api/v1")
