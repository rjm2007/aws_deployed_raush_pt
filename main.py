from fastapi import FastAPI

from app.api import availability, appointments, leads, debug

app = FastAPI(title="Rausch PT Tebra API")

app.include_router(availability.router)
app.include_router(appointments.router)
app.include_router(leads.router)
app.include_router(debug.router)
