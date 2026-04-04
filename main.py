from fastapi import FastAPI

from app.api import availability, appointments, leads, system

app = FastAPI(
    title="Rausch PT Tebra API",
    version="1.0.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.include_router(availability.router,  prefix="/api/v1")
app.include_router(appointments.router,  prefix="/api/v1")
app.include_router(leads.router,         prefix="/api/v1")
app.include_router(system.router,        prefix="/api/v1")
