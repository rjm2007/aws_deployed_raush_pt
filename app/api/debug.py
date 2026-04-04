import os
import re
import httpx

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse

from app.core.config import (
    TEBRA_URL, CUSTOMER_KEY, TEBRA_PASSWORD, TEBRA_USER,
    PRACTICE_ID, LOCATION_MAP,
)
from app.services.tebra_service import call_tebra_get_appointments
from app.utils.time_utils import parse_booked_slots, format_12hr
from app.utils.parser import parse_soap_fault

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "Rausch PT Tebra API"}


@router.get("/test-tebra")
async def test_tebra():
    xml    = await call_tebra_get_appointments("2026-04-10", "Rausch Dana Point")
    booked = parse_booked_slots(xml, "2026-04-10")
    return {
        "raw_xml_length": len(xml),
        "booked_slots":   [format_12hr(h, mn) for h, mn in sorted(booked)],
        "booked_count":   len(booked),
    }


@router.get("/debug-appointment-reasons")
async def debug_appointment_reasons():
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:GetAppointmentReasons>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:PracticeId>{PRACTICE_ID}</sch:PracticeId>
      </sch:request>
    </sch:GetAppointmentReasons>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetAppointmentReasons",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r   = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text
    fault = parse_soap_fault(xml)
    if fault:
        return {"error": "SOAP Fault", "detail": fault}
    reasons = []
    for block in re.findall(r'<AppointmentReasonData>(.*?)</AppointmentReasonData>', xml, re.DOTALL):
        rid_  = re.search(r'<AppointmentReasonId>(\d+)</AppointmentReasonId>', block)
        name  = re.search(r'<Name>([^<]+)</Name>', block)
        dur   = re.search(r'<DefaultDurationMinutes>(\d+)</DefaultDurationMinutes>', block)
        reasons.append({
            "id":       rid_.group(1) if rid_  else None,
            "name":     name.group(1) if name  else None,
            "duration": dur.group(1)  if dur   else None,
        })
    return {"total": len(reasons), "reasons": reasons}


@router.get("/debug-providers")
async def debug_providers():
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:GetProviders>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Fields>
          <sch:ID>true</sch:ID>
          <sch:FirstName>true</sch:FirstName>
          <sch:LastName>true</sch:LastName>
          <sch:FullName>true</sch:FullName>
          <sch:Active>true</sch:Active>
          <sch:Type>true</sch:Type>
        </sch:Fields>
        <sch:Filter>
          <sch:PracticeID>{PRACTICE_ID}</sch:PracticeID>
          <sch:Type>Normal Provider</sch:Type>
        </sch:Filter>
      </sch:request>
    </sch:GetProviders>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetProviders",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r   = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text
    fault = parse_soap_fault(xml)
    if fault:
        return {"error": "SOAP Fault", "detail": fault}
    providers = []
    for block in re.findall(r'<ProviderData>(.*?)</ProviderData>', xml, re.DOTALL):
        pid_     = re.search(r'<ID>([^<]+)</ID>', block)
        fname    = re.search(r'<FirstName>([^<]+)</FirstName>', block)
        lname    = re.search(r'<LastName>([^<]+)</LastName>', block)
        fullname = re.search(r'<FullName>([^<]+)</FullName>', block)
        active   = re.search(r'<Active>([^<]+)</Active>', block)
        is_active = active and active.group(1).lower() == 'true'
        has_name  = bool(
            (fullname and fullname.group(1).strip()) or
            (fname and fname.group(1).strip()) or
            (lname and lname.group(1).strip())
        )
        if is_active and has_name:
            providers.append({
                "provider_id": pid_.group(1)     if pid_     else None,
                "full_name":   fullname.group(1) if fullname else None,
                "first_name":  fname.group(1)    if fname    else None,
                "last_name":   lname.group(1)    if lname    else None,
            })
    return {"total_active": len(providers), "providers": providers}


@router.get("/debug-resource-ids")
async def debug_resource_ids(date: str = "2026-04-08"):
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:GetAppointments>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Fields>
          <sch:ID>true</sch:ID>
          <sch:StartDate>true</sch:StartDate>
          <sch:ServiceLocationName>true</sch:ServiceLocationName>
          <sch:PatientFullName>true</sch:PatientFullName>
          <sch:ResourceID1>true</sch:ResourceID1>
          <sch:ResourceID2>true</sch:ResourceID2>
          <sch:ResourceName1>true</sch:ResourceName1>
          <sch:ResourceName2>true</sch:ResourceName2>
          <sch:ResourceTypeID1>true</sch:ResourceTypeID1>
          <sch:AppointmentReason1>true</sch:AppointmentReason1>
        </sch:Fields>
        <sch:Filter>
          <sch:StartDate>{date}T00:00:00</sch:StartDate>
          <sch:EndDate>{date}T23:59:59</sch:EndDate>
        </sch:Filter>
      </sch:request>
    </sch:GetAppointments>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetAppointments",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r   = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text
    fault = parse_soap_fault(xml)
    if fault:
        return {"error": "SOAP Fault", "detail": fault}
    appointments = []
    for block in re.findall(r'<AppointmentData>(.*?)</AppointmentData>', xml, re.DOTALL):
        appt_id = re.search(r'<ID>([^<]+)</ID>', block)
        start   = re.search(r'<StartDate>([^<]+)</StartDate>', block)
        loc_    = re.search(r'<ServiceLocationName>([^<]+)</ServiceLocationName>', block)
        patient = re.search(r'<PatientFullName>([^<]+)</PatientFullName>', block)
        r1_id   = re.search(r'<ResourceID1>([^<]+)</ResourceID1>', block)
        r1_name = re.search(r'<ResourceName1>([^<]+)</ResourceName1>', block)
        r1_type = re.search(r'<ResourceTypeID1>([^<]+)</ResourceTypeID1>', block)
        r2_id   = re.search(r'<ResourceID2>([^<]+)</ResourceID2>', block)
        reason  = re.search(r'<AppointmentReason1>([^<]+)</AppointmentReason1>', block)
        appointments.append({
            "appointment_id":  appt_id.group(1) if appt_id else None,
            "start":           start.group(1)   if start   else None,
            "location":        loc_.group(1)    if loc_    else None,
            "patient":         patient.group(1) if patient else None,
            "resource_id_1":   r1_id.group(1)   if r1_id   else None,
            "resource_name_1": r1_name.group(1) if r1_name else None,
            "resource_type_1": r1_type.group(1) if r1_type else None,
            "resource_id_2":   r2_id.group(1)   if r2_id   else None,
            "reason":          reason.group(1)  if reason  else None,
        })
    return {"date": date, "total": len(appointments), "appointments": appointments}


@router.get("/debug-location-providers")
async def debug_location_providers():
    """Fetch actual providers per location from real Tebra appointments (March data)."""
    results    = {}
    start_date = "2026-03-01"
    end_date   = "2026-03-31"

    for label, loc in LOCATION_MAP.items():
        tebra_name = loc["name"]
        soap_body  = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:GetAppointments>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Fields>
          <sch:ID>true</sch:ID>
          <sch:ResourceID1>true</sch:ResourceID1>
          <sch:ResourceName1>true</sch:ResourceName1>
          <sch:ResourceTypeID1>true</sch:ResourceTypeID1>
          <sch:ServiceLocationName>true</sch:ServiceLocationName>
          <sch:StartDate>true</sch:StartDate>
        </sch:Fields>
        <sch:Filter>
          <sch:StartDate>{start_date}T00:00:00</sch:StartDate>
          <sch:EndDate>{end_date}T23:59:59</sch:EndDate>
          <sch:ServiceLocationName>{tebra_name}</sch:ServiceLocationName>
        </sch:Filter>
      </sch:request>
    </sch:GetAppointments>
  </soapenv:Body>
</soapenv:Envelope>"""
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction":   "http://www.kareo.com/api/schemas/KareoServices/GetAppointments",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            r   = await client.post(TEBRA_URL, content=soap_body, headers=headers)
            xml = r.text
        providers_seen = {}
        for block in re.findall(r'<AppointmentData>(.*?)</AppointmentData>', xml, re.DOTALL):
            r1_id   = re.search(r'<ResourceID1>([^<]+)</ResourceID1>', block)
            r1_name = re.search(r'<ResourceName1>([^<]+)</ResourceName1>', block)
            r1_type = re.search(r'<ResourceTypeID1>([^<]+)</ResourceTypeID1>', block)
            if r1_id and r1_name:
                rid_val = r1_id.group(1)
                if rid_val not in providers_seen:
                    rtype = r1_type.group(1) if r1_type else None
                    providers_seen[rid_val] = {
                        "resource_id":   rid_val,
                        "resource_name": r1_name.group(1),
                        "resource_type": "Doctor" if rtype == "1" else "Practice Resource",
                    }
        results[label] = {
            "location_id":     loc["id"],
            "providers_found": len(providers_seen),
            "providers":       list(providers_seen.values()),
        }
    return results


@router.get("/logs")
async def view_logs(lines: int = 300):
    """Stream the last N lines of the tebra_debug.log file.
    Usage: GET /logs          → last 300 lines
           GET /logs?lines=500 → last 500 lines
    """
    log_path = "logs/tebra_debug.log"
    if not os.path.exists(log_path):
        return PlainTextResponse("Log file not found. No requests have been processed yet.")
    with open(log_path, "r", encoding="utf-8") as f:
        all_lines = f.readlines()
    tail = all_lines[-lines:]
    return PlainTextResponse("".join(tail))
