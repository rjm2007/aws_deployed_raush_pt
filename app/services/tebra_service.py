import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from xml.sax.saxutils import escape

from app.core.config import (
    TEBRA_URL, CUSTOMER_KEY, TEBRA_PASSWORD, TEBRA_USER,
    PRACTICE_ID, PROVIDER_ID, RESOURCE_ID,
    DEFAULT_SLOT_MINUTES,
    TEBRA_PRACTICE_NAME,
    TEBRA_INBOUND_APPOINTMENTS_PRACTICE_NAME,
)
from app.core.logger import logger
from app.utils.time_utils import (
    CLINIC_TZ,
    clinic_utc_offset_hours,
    format_12hr,
    la_to_utc,
    parse_tebra_datetime,
    parse_tebra_local_start_datetime,
    parse_time_to_24hr,
)
from app.utils.parser import parse_soap_fault


async def call_tebra_get_appointments(date: str, tebra_location_name: str) -> str:
    """One clinic, one day. Times come back in clinic local time for THAT date (DST-aware offset).
    Fields/Filter are alphabetical (Tebra/WCF ignores out-of-order elements)."""
    tz = clinic_utc_offset_hours(date)
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:GetAppointments>
      <sch:request>
        <sch:RequestHeader>
          <sch:ClientVersion>1</sch:ClientVersion>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Fields>
          <sch:AppointmentDuration>true</sch:AppointmentDuration>
          <sch:AppointmentReason1>true</sch:AppointmentReason1>
          <sch:ConfirmationStatus>true</sch:ConfirmationStatus>
          <sch:EndDate>true</sch:EndDate>
          <sch:ID>true</sch:ID>
          <sch:PatientFullName>true</sch:PatientFullName>
          <sch:PatientID>true</sch:PatientID>
          <sch:ServiceLocationName>true</sch:ServiceLocationName>
          <sch:StartDate>true</sch:StartDate>
        </sch:Fields>
        <sch:Filter>
          <sch:EndDate>{date}T23:59:59</sch:EndDate>
          <sch:ServiceLocationName>{escape(tebra_location_name or "")}</sch:ServiceLocationName>
          <sch:StartDate>{date}T00:00:00</sch:StartDate>
          <sch:TimeZoneOffsetFromGMT>{tz}</sch:TimeZoneOffsetFromGMT>
        </sch:Filter>
      </sch:request>
    </sch:GetAppointments>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetAppointments",
    }
    logger.info("[tebra] GetAppointments date=%s location=%s tz=%s", date, tebra_location_name, tz)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        logger.info("[tebra] GetAppointments status=%s length=%s", r.status_code, len(r.text))
        logger.info("[tebra] GetAppointments raw=%s", r.text)
        return r.text


_APPT_DATA_TAGS = (
    "ID",
    "PatientID",
    "PatientFullName",
    "StartDate",
    "EndDate",
    "AppointmentDuration",
    "AppointmentReason1",
    "ConfirmationStatus",
    "ResourceName1",
    "ServiceLocationName",
    "Notes",
    "PracticeName",
)


def _extract_tag(block: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", block)
    if not m:
        return None
    s = m.group(1).strip()
    return s if s else None


def _parse_get_appointments_blocks(xml: str, times_are_utc: bool = False) -> list[dict]:
    """times_are_utc=True: the request used TimeZoneOffsetFromGMT=0, so StartDate is UTC and is
    converted to clinic time per row (correct on both sides of a DST change)."""
    blocks = re.findall(r"<AppointmentData>(.*?)</AppointmentData>", xml, re.DOTALL)
    out = []
    for b in blocks:
        row = {t: _extract_tag(b, t) for t in _APPT_DATA_TAGS}
        tid = row.get("ID")
        if not tid:
            continue
        parsed = None
        sd = row.get("StartDate")
        if sd and times_are_utc:
            naive = parse_tebra_datetime(sd)
            if naive:
                la = naive.replace(tzinfo=timezone.utc).astimezone(CLINIC_TZ)
                parsed = (la.strftime("%Y-%m-%d"), la.hour, la.minute)
        elif sd:
            parsed = parse_tebra_local_start_datetime(sd)
        appt_date = parsed[0] if parsed else None
        time_12 = format_12hr(parsed[1], parsed[2]) if parsed else None
        time_24 = f"{parsed[1]:02d}:{parsed[2]:02d}" if parsed else None
        out.append({
            "tebra_appointment_id": tid,
            "patient_id":           row.get("PatientID"),
            "patient_full_name":    row.get("PatientFullName"),
            "start_date_raw":       sd,
            "appointment_date":     appt_date,
            "appointment_time_12hr": time_12,
            "appointment_time_24hr": time_24,
            "end_date_raw":         row.get("EndDate"),
            "service_location_name": row.get("ServiceLocationName"),
            "appointment_reason":   row.get("AppointmentReason1"),
            "confirmation_status":  row.get("ConfirmationStatus"),
            "resource_name":        row.get("ResourceName1"),
            "notes":                row.get("Notes"),
            "practice_name":        row.get("PracticeName"),
        })
    return out


async def call_tebra_get_appointments_by_patient(
    patient_full_name: str,
    start_date: str,
    end_date: str,
    timezone_offset_from_gmt: int | None = None,
    rid: str = "?",
) -> dict:
    """
    Tebra GetAppointments filtered by patient name and calendar date range (inclusive).
    start_date / end_date: YYYY-MM-DD.
    """
    # Default: ask Tebra for UTC (offset 0) and convert each row to clinic time, so a range
    # that crosses a DST change is right on both sides. An explicit override keeps the old behaviour.
    tz = timezone_offset_from_gmt if timezone_offset_from_gmt is not None else 0
    safe_name = escape((patient_full_name or "").strip())
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:GetAppointments>
      <sch:request>
        <sch:RequestHeader>
          <sch:ClientVersion>1</sch:ClientVersion>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Fields>
          <sch:AppointmentDuration>true</sch:AppointmentDuration>
          <sch:AppointmentReason1>true</sch:AppointmentReason1>
          <sch:ConfirmationStatus>true</sch:ConfirmationStatus>
          <sch:EndDate>true</sch:EndDate>
          <sch:ID>true</sch:ID>
          <sch:Notes>true</sch:Notes>
          <sch:PatientFullName>true</sch:PatientFullName>
          <sch:PatientID>true</sch:PatientID>
          <sch:PracticeName>true</sch:PracticeName>
          <sch:ResourceName1>true</sch:ResourceName1>
          <sch:ServiceLocationName>true</sch:ServiceLocationName>
          <sch:StartDate>true</sch:StartDate>
        </sch:Fields>
        <sch:Filter>
          <sch:EndDate>{end_date}</sch:EndDate>
          <sch:PatientFullName>{safe_name}</sch:PatientFullName>
          <sch:StartDate>{start_date}</sch:StartDate>
          <sch:TimeZoneOffsetFromGMT>{tz}</sch:TimeZoneOffsetFromGMT>
        </sch:Filter>
      </sch:request>
    </sch:GetAppointments>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetAppointments",
    }
    logger.info(
        "[%s] GetAppointments by patient name=%s range=%s..%s tz=%s",
        rid, patient_full_name, start_date, end_date, tz,
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text
    logger.info("[%s] GetAppointments-by-patient status=%s len=%s", rid, r.status_code, len(xml))

    fault = parse_soap_fault(xml)
    if fault:
        logger.error("[%s] GetAppointments-by-patient fault=%s", rid, fault)
        return {"ok": False, "fault": fault, "error_message": fault, "appointments": []}

    is_error = re.search(r"<IsError>(true|false)</IsError>", xml)
    if is_error and is_error.group(1).lower() == "true":
        err = re.search(r"<ErrorMessage>([^<]*)</ErrorMessage>", xml)
        msg = (err.group(1).strip() if err else "") or "Tebra GetAppointments error"
        logger.error("[%s] GetAppointments-by-patient IsError=%s", rid, msg)
        return {"ok": False, "fault": None, "error_message": msg, "appointments": []}

    auth = re.search(r"<Authenticated>(true|false)</Authenticated>", xml)
    if auth and auth.group(1).lower() != "true":
        logger.error("[%s] GetAppointments-by-patient not authenticated", rid)
        return {"ok": False, "fault": None, "error_message": "Not authenticated with Tebra", "appointments": []}

    appointments = _parse_get_appointments_blocks(xml, times_are_utc=(tz == 0))
    return {"ok": True, "fault": None, "error_message": None, "appointments": appointments}


async def call_tebra_get_appointments_by_patient_id(
    patient_id: str,
    start_date: str,
    end_date: str,
    practice_name: str | None = None,
    timezone_offset_from_gmt: int | None = None,
    rid: str = "?",
) -> dict:
    """
    Tebra GetAppointments filtered by PatientID + PracticeName + calendar date range (inclusive).
    start_date / end_date: YYYY-MM-DD (clinic calendar dates).
    """
    # Default: ask Tebra for UTC (offset 0) and convert each row to clinic time, so a range
    # that crosses a DST change is right on both sides. An explicit override keeps the old behaviour.
    tz = timezone_offset_from_gmt if timezone_offset_from_gmt is not None else 0
    pn = (practice_name or TEBRA_INBOUND_APPOINTMENTS_PRACTICE_NAME or "").strip()
    safe_practice = escape(pn)
    pid = (patient_id or "").strip()
    if not pid.isdigit():
        return {"ok": False, "fault": None, "error_message": "Invalid patient_id", "appointments": []}
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:GetAppointments>
      <sch:request>
        <sch:RequestHeader>
          <sch:ClientVersion>1</sch:ClientVersion>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Fields>
          <sch:AppointmentDuration>true</sch:AppointmentDuration>
          <sch:AppointmentReason1>true</sch:AppointmentReason1>
          <sch:ConfirmationStatus>true</sch:ConfirmationStatus>
          <sch:EndDate>true</sch:EndDate>
          <sch:ID>true</sch:ID>
          <sch:Notes>true</sch:Notes>
          <sch:PatientFullName>true</sch:PatientFullName>
          <sch:PatientID>true</sch:PatientID>
          <sch:PracticeName>true</sch:PracticeName>
          <sch:ResourceName1>true</sch:ResourceName1>
          <sch:ServiceLocationName>true</sch:ServiceLocationName>
          <sch:StartDate>true</sch:StartDate>
        </sch:Fields>
        <sch:Filter>
          <sch:EndDate>{end_date}</sch:EndDate>
          <sch:PatientID>{pid}</sch:PatientID>
          <sch:PracticeName>{safe_practice}</sch:PracticeName>
          <sch:StartDate>{start_date}</sch:StartDate>
          <sch:TimeZoneOffsetFromGMT>{tz}</sch:TimeZoneOffsetFromGMT>
        </sch:Filter>
      </sch:request>
    </sch:GetAppointments>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetAppointments",
    }
    logger.info(
        "[%s] GetAppointments by patientId=%s practice=%s range=%s..%s tz=%s",
        rid, patient_id, pn, start_date, end_date, tz,
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text
    logger.info("[%s] GetAppointments-by-patient-id status=%s len=%s", rid, r.status_code, len(xml))

    fault = parse_soap_fault(xml)
    if fault:
        logger.error("[%s] GetAppointments-by-patient-id fault=%s", rid, fault)
        return {"ok": False, "fault": fault, "error_message": fault, "appointments": []}

    is_error = re.search(r"<IsError>(true|false)</IsError>", xml)
    if is_error and is_error.group(1).lower() == "true":
        err = re.search(r"<ErrorMessage>([^<]*)</ErrorMessage>", xml)
        msg = (err.group(1).strip() if err else "") or "Tebra GetAppointments error"
        logger.error("[%s] GetAppointments-by-patient-id IsError=%s", rid, msg)
        return {"ok": False, "fault": None, "error_message": msg, "appointments": []}

    auth = re.search(r"<Authenticated>(true|false)</Authenticated>", xml)
    if auth and auth.group(1).lower() != "true":
        logger.error("[%s] GetAppointments-by-patient-id not authenticated", rid)
        return {"ok": False, "fault": None, "error_message": "Not authenticated with Tebra", "appointments": []}

    appointments = _parse_get_appointments_blocks(xml, times_are_utc=(tz == 0))
    return {"ok": True, "fault": None, "error_message": None, "appointments": appointments}


async def get_patient_by_name(first_name: str, last_name: str, rid: str = "?") -> str | None:
    """First patient ID with this exact first + last name in the practice (PracticeID + PracticeName
    filters are required by Tebra; verified 2026-10-30 they return the same IDs as before)."""
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:GetPatients>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Fields>
          <sch:FirstName>true</sch:FirstName>
          <sch:ID>true</sch:ID>
          <sch:LastName>true</sch:LastName>
          <sch:MobilePhone>true</sch:MobilePhone>
        </sch:Fields>
        <sch:Filter>
          <sch:FirstName>{escape((first_name or "").strip())}</sch:FirstName>
          <sch:LastName>{escape((last_name or "").strip())}</sch:LastName>
          <sch:PracticeID>{PRACTICE_ID}</sch:PracticeID>
          <sch:PracticeName>{escape(TEBRA_PRACTICE_NAME)}</sch:PracticeName>
        </sch:Filter>
      </sch:request>
    </sch:GetPatients>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetPatients",
    }
    logger.info("[%s] GetPatients firstName=%s lastName=%s", rid, first_name, last_name)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text
    logger.info("[%s] GetPatients raw=%s", rid, xml)
    m = re.search(r'<ID>(\d+)</ID>', xml)
    if m:
        logger.info("[%s] GetPatients found patientId=%s", rid, m.group(1))
        return m.group(1)
    logger.info("[%s] GetPatients not found", rid)
    return None


async def create_patient(first_name: str, last_name: str, phone: str, rid: str = "?") -> str | None:
    # Tebra requires max 10 chars for MobilePhone — strip to last 10 digits
    digits      = re.sub(r'[^\d]', '', phone or '')
    tebra_phone = digits[-10:] if len(digits) > 10 else digits
    if tebra_phone != digits:
        logger.info("[%s] CreatePatient phone stripped for Tebra: %s → %s", rid, phone, tebra_phone)

    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:CreatePatient>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Patient>
          <sch:FirstName>{first_name}</sch:FirstName>
          <sch:LastName>{last_name}</sch:LastName>
          <sch:MobilePhone>{tebra_phone}</sch:MobilePhone>
          <sch:Practice>
            <sch:PracticeID>{PRACTICE_ID}</sch:PracticeID>
          </sch:Practice>
        </sch:Patient>
      </sch:request>
    </sch:CreatePatient>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/CreatePatient",
    }
    logger.info("[%s] CreatePatient firstName=%s lastName=%s phone=%s", rid, first_name, last_name, phone)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text
    logger.info("[%s] CreatePatient raw=%s", rid, xml)

    fault = parse_soap_fault(xml)
    if fault:
        logger.error("[%s] CreatePatient fault=%s", rid, fault)
        return None

    is_error = re.search(r'<IsError>(true|false)</IsError>', xml)
    if is_error and is_error.group(1).lower() == 'true':
        err = re.search(r'<ErrorMessage>([^<]+)</ErrorMessage>', xml)
        logger.error("[%s] CreatePatient error=%s", rid, err.group(1) if err else "unknown")
        return None

    m = re.search(r'<PatientID>(\d+)</PatientID>', xml)
    if m:
        logger.info("[%s] ✓ PATIENT CREATED IN TEBRA | patientId=%s firstName=%s lastName=%s phone=%s",
                    rid, m.group(1), first_name, last_name, phone)
        return m.group(1)

    logger.error("[%s] CreatePatient patientId not found in response — raw=%s", rid, xml[:500])
    return None


async def create_appointment_in_tebra(
    patient_id: str,
    location_id: str,
    date: str,
    time_str: str,
    appointment_reason_id: str,
    rid: str = "?",
    provider_id: str | int | None = None,
    duration_minutes: int = DEFAULT_SLOT_MINUTES,
    notes: str | None = None,
) -> dict:
    """
    Old endpoints call this with the defaults (PROVIDER_ID from .env, 30 min) — unchanged.
    Multi-provider: pass provider_id (Tebra clinician ID) + duration_minutes (60 eval / 45 follow-up).
    ResourceId stays RESOURCE_ID (1 = practice resource): ResourceId = provider ID fails in Tebra
    ("Error translating AppointmentCreate"); proven working shape = appt 37018 (2026-10-29).
    Start/end are converted with the real LA offset for that date (DST-safe).
    """
    parsed = parse_time_to_24hr(time_str)
    if not parsed:
        return {"success": False, "appointment_id": None, "error": f"Could not parse time: {time_str}"}

    h, mn     = parsed
    start_utc = la_to_utc(date, h, mn)
    end_utc   = start_utc + timedelta(minutes=int(duration_minutes or DEFAULT_SLOT_MINUTES))
    start_dt  = start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_dt    = end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    prov_id   = str(provider_id) if provider_id not in (None, "") else PROVIDER_ID
    notes_xml = f"\n          <sch:Notes>{escape(notes)}</sch:Notes>" if notes else ""

    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/"
  xmlns:arr="http://schemas.microsoft.com/2003/10/Serialization/Arrays">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:CreateAppointment>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Appointment>
          <sch:AppointmentReasonId>{appointment_reason_id}</sch:AppointmentReasonId>
          <sch:AppointmentStatus>Scheduled</sch:AppointmentStatus>
          <sch:AppointmentType>P</sch:AppointmentType>
          <sch:EndTime>{end_dt}</sch:EndTime>
          <sch:IsRecurring>false</sch:IsRecurring>{notes_xml}
          <sch:PatientSummary>
            <sch:PatientId>{patient_id}</sch:PatientId>
          </sch:PatientSummary>
          <sch:PracticeId>{PRACTICE_ID}</sch:PracticeId>
          <sch:ProviderId>{prov_id}</sch:ProviderId>
          <sch:ResourceId>{RESOURCE_ID}</sch:ResourceId>
          <sch:ResourceIds>
            <arr:long>{RESOURCE_ID}</arr:long>
          </sch:ResourceIds>
          <sch:ServiceLocationId>{location_id}</sch:ServiceLocationId>
          <sch:StartTime>{start_dt}</sch:StartTime>
          <sch:WasCreatedOnline>false</sch:WasCreatedOnline>
        </sch:Appointment>
      </sch:request>
    </sch:CreateAppointment>
  </soapenv:Body>
</soapenv:Envelope>"""

    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/CreateAppointment",
    }

    logger.info("[%s] CreateAppointment patientId=%s locationId=%s reasonId=%s providerId=%s minutes=%s start=%s end=%s",
                rid, patient_id, location_id, appointment_reason_id, prov_id, duration_minutes, start_dt, end_dt)

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text

    logger.info("[%s] CreateAppointment status=%s length=%s", rid, r.status_code, len(xml))
    logger.info("[%s] CreateAppointment raw=%s", rid, xml)

    fault = parse_soap_fault(xml)
    if fault:
        logger.error("[%s] CreateAppointment soap_fault=%s", rid, fault)
        return {"success": False, "appointment_id": None, "error": fault}

    is_error = re.search(r'<IsError>(true|false)</IsError>', xml)
    if is_error and is_error.group(1).lower() == 'true':
        err   = re.search(r'<ErrorMessage>([^<]+)</ErrorMessage>', xml)
        error = err.group(1) if err else "Unknown Tebra error"
        logger.error("[%s] CreateAppointment tebra_error=%s", rid, error)
        return {"success": False, "appointment_id": None, "error": error}

    m = re.search(r'<AppointmentId>(\d+)</AppointmentId>', xml)
    if m:
        logger.info("[%s] CreateAppointment SUCCESS appointmentId=%s", rid, m.group(1))
        return {"success": True, "appointment_id": m.group(1), "error": None}

    logger.error("[%s] CreateAppointment appointmentId not found in response", rid)
    return {"success": False, "appointment_id": None, "error": "AppointmentId not found in response"}


async def call_tebra_get_appointment(tebra_appt_id: str, rid: str = "?") -> dict | None:
    """Fetch a single appointment from Tebra by its AppointmentId.
    Returns a dict with extracted fields, or None on failure."""
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:GetAppointment>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Appointment>
          <sch:AppointmentId>{tebra_appt_id}</sch:AppointmentId>
        </sch:Appointment>
      </sch:request>
    </sch:GetAppointment>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetAppointment",
    }
    logger.info("[%s] GetAppointment tebra_appt_id=%s", rid, tebra_appt_id)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text

    logger.info("[%s] GetAppointment status=%s length=%s", rid, r.status_code, len(xml))
    logger.info("[%s] GetAppointment raw=%s", rid, xml)

    fault = parse_soap_fault(xml)
    if fault:
        logger.error("[%s] GetAppointment soap_fault=%s", rid, fault)
        return None

    is_error = re.search(r'<IsError>(true|false)</IsError>', xml)
    if is_error and is_error.group(1).lower() == 'true':
        err = re.search(r'<ErrorMessage>([^<]+)</ErrorMessage>', xml)
        logger.error("[%s] GetAppointment error=%s", rid, err.group(1) if err else "unknown")
        return None

    def extract(tag):
        m = re.search(rf'<{tag}>([^<]+)</{tag}>', xml)
        return m.group(1) if m else None

    # PatientId is nested inside <PatientSummary>
    patient_id_match = re.search(r'<PatientSummary>.*?<PatientId>(\d+)</PatientId>.*?</PatientSummary>', xml, re.DOTALL)
    patient_id = patient_id_match.group(1) if patient_id_match else extract("PatientId")

    data = {
        "AppointmentId":       tebra_appt_id,
        "AppointmentName":     extract("AppointmentName") or "",
        "AppointmentReasonId": extract("AppointmentReasonId") or "0",
        "AppointmentStatus":   extract("AppointmentStatus") or "Scheduled",
        "EndTime":             extract("EndTime"),
        "IsRecurring":         extract("IsRecurring") or "false",
        "MaxAttendees":        extract("MaxAttendees") or "1",
        "PatientId":           patient_id,
        "ProviderId":          extract("ProviderId") or PROVIDER_ID,
        "ResourceId":          extract("ResourceId") or RESOURCE_ID,
        "ServiceLocationId":   extract("ServiceLocationId"),
        "StartTime":           extract("StartTime"),
    }

    # Extract ResourceIds (may have multiple)
    resource_ids = re.findall(r'<a:long>(\d+)</a:long>', xml)
    data["ResourceIds"] = resource_ids if resource_ids else [data["ResourceId"]]

    logger.info("[%s] GetAppointment parsed data=%s", rid, data)
    return data


async def call_tebra_delete_appointment(tebra_appt_id: str, rid: str = "?") -> dict:
    """Remove an appointment from the Tebra schedule (SOAP DeleteAppointment)."""
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:DeleteAppointment>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Appointment>
          <sch:AppointmentId>{tebra_appt_id}</sch:AppointmentId>
        </sch:Appointment>
      </sch:request>
    </sch:DeleteAppointment>
  </soapenv:Body>
</soapenv:Envelope>"""

    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/DeleteAppointment",
    }

    logger.info("[%s] DeleteAppointment apptId=%s", rid, tebra_appt_id)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text

    logger.info("[%s] DeleteAppointment status=%s length=%s", rid, r.status_code, len(xml))
    logger.info("[%s] DeleteAppointment raw=%s", rid, xml)

    fault = parse_soap_fault(xml)
    if fault:
        logger.error("[%s] DeleteAppointment soap_fault=%s", rid, fault)
        return {"success": False, "error": fault}

    is_error = re.search(r'<IsError>(true|false)</IsError>', xml)
    if is_error and is_error.group(1).lower() == "true":
        err = re.search(r"<ErrorMessage>([^<]+)</ErrorMessage>", xml)
        error = err.group(1) if err else "Unknown Tebra error"
        logger.error("[%s] DeleteAppointment tebra_error=%s", rid, error)
        return {"success": False, "error": error}

    deleted = re.search(r"<Deleted>(true|false)</Deleted>", xml)
    if deleted and deleted.group(1).lower() == "false":
        logger.error("[%s] DeleteAppointment Deleted=false", rid)
        return {"success": False, "error": "Tebra reported the appointment was not deleted"}

    logger.info("[%s] DeleteAppointment SUCCESS", rid)
    return {"success": True, "error": None}


async def call_tebra_update_appointment(appt_data: dict, rid: str = "?") -> dict:
    """Update an appointment in Tebra.
    appt_data must contain all required fields from GetAppointment.
    Field order follows exact XSD schema sequence for AppointmentUpdate."""
    resource_ids_xml = ""
    for res_id in appt_data.get("ResourceIds", []):
        resource_ids_xml += f"            <arr:long>{res_id}</arr:long>\n"

    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/"
  xmlns:arr="http://schemas.microsoft.com/2003/10/Serialization/Arrays">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:UpdateAppointment>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{CUSTOMER_KEY}</sch:CustomerKey>
          <sch:Password>{TEBRA_PASSWORD}</sch:Password>
          <sch:User>{TEBRA_USER}</sch:User>
        </sch:RequestHeader>
        <sch:Appointment>
          <sch:AppointmentId>{appt_data["AppointmentId"]}</sch:AppointmentId>
          <sch:AppointmentName>{appt_data.get("AppointmentName", "")}</sch:AppointmentName>
          <sch:AppointmentReasonId>{appt_data.get("AppointmentReasonId", "0")}</sch:AppointmentReasonId>
          <sch:AppointmentStatus>{appt_data["AppointmentStatus"]}</sch:AppointmentStatus>
          <sch:EndTime>{appt_data["EndTime"]}</sch:EndTime>
          <sch:IsRecurring>{appt_data.get("IsRecurring", "false")}</sch:IsRecurring>
          <sch:MaxAttendees>{appt_data.get("MaxAttendees", "1")}</sch:MaxAttendees>
          <sch:PatientId>{appt_data["PatientId"]}</sch:PatientId>
          <sch:ProviderId>{appt_data.get("ProviderId", PROVIDER_ID)}</sch:ProviderId>
          <sch:ResourceId>{appt_data.get("ResourceId", RESOURCE_ID)}</sch:ResourceId>
          <sch:ResourceIds>
{resource_ids_xml}          </sch:ResourceIds>
          <sch:ServiceLocationId>{appt_data["ServiceLocationId"]}</sch:ServiceLocationId>
          <sch:StartTime>{appt_data["StartTime"]}</sch:StartTime>
        </sch:Appointment>
      </sch:request>
    </sch:UpdateAppointment>
  </soapenv:Body>
</soapenv:Envelope>"""

    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/UpdateAppointment",
    }

    logger.info("[%s] UpdateAppointment apptId=%s newStatus=%s start=%s end=%s",
                rid, appt_data["AppointmentId"], appt_data["AppointmentStatus"],
                appt_data["StartTime"], appt_data["EndTime"])

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text

    logger.info("[%s] UpdateAppointment status=%s length=%s", rid, r.status_code, len(xml))
    logger.info("[%s] UpdateAppointment raw=%s", rid, xml)

    fault = parse_soap_fault(xml)
    if fault:
        logger.error("[%s] UpdateAppointment soap_fault=%s", rid, fault)
        return {"success": False, "error": fault}

    is_error = re.search(r'<IsError>(true|false)</IsError>', xml)
    if is_error and is_error.group(1).lower() == 'true':
        err = re.search(r'<ErrorMessage>([^<]+)</ErrorMessage>', xml)
        error = err.group(1) if err else "Unknown Tebra error"
        logger.error("[%s] UpdateAppointment tebra_error=%s", rid, error)
        return {"success": False, "error": error}

    logger.info("[%s] UpdateAppointment SUCCESS", rid)
    return {"success": True, "error": None}


# ─── Practice-wide calendar (multi-provider sync + live slot checks) ──────────

_PRACTICE_APPT_FIELDS = (   # alphabetical — Tebra/WCF ignores out-of-order elements
    "AppointmentDuration", "AppointmentReasonID1", "ConfirmationStatus", "EndDate", "ID",
    "PatientID", "ResourceID1", "ResourceID2", "ResourceTypeID1", "ResourceTypeID2",
    "ServiceLocationID", "StartDate", "Type",
)

# GetAppointments limit is 1 call/second per account. One lock per process; a 429 is retried once.
_GET_APPTS_LOCK = asyncio.Lock()
_GET_APPTS_LAST = 0.0
_GET_APPTS_MIN_GAP = 1.1


async def _post_get_appointments_throttled(soap_body: str, rid: str) -> httpx.Response:
    global _GET_APPTS_LAST
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetAppointments",
    }
    async with _GET_APPTS_LOCK:
        for attempt in (1, 2):
            wait = _GET_APPTS_MIN_GAP - (time.monotonic() - _GET_APPTS_LAST)
            if wait > 0:
                await asyncio.sleep(wait)
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
            _GET_APPTS_LAST = time.monotonic()
            if r.status_code != 429 or attempt == 2:
                return r
            logger.warning("[%s] GetAppointments 429 — retrying in 3s", rid)
            await asyncio.sleep(3)
    return r


async def call_tebra_get_practice_appointments(start_date: str, end_date: str, rid: str = "?") -> dict:
    """
    Every appointment in the practice between two dates (YYYY-MM-DD, inclusive), all clinics and
    clinicians. Asks Tebra for UTC (TimeZoneOffsetFromGMT=0), so the date filter is in UTC days —
    callers pad the range by a day and filter on start_utc. Max 10,000 rows per call: keep ranges <= 7 days.
    Returns {"ok", "error_message", "rows": [{id, start_utc, end_utc, duration, status, type,
    location_id, patient_id, reason_id, resources: [(id, type_id), ...]}]}. No patient names are requested.
    """
    fields_xml = "".join(f"\n          <sch:{f}>true</sch:{f}>" for f in _PRACTICE_APPT_FIELDS)
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:GetAppointments>
      <sch:request>
        <sch:RequestHeader>
          <sch:ClientVersion>1</sch:ClientVersion>
          <sch:CustomerKey>{escape(CUSTOMER_KEY or "")}</sch:CustomerKey>
          <sch:Password>{escape(TEBRA_PASSWORD or "")}</sch:Password>
          <sch:User>{escape(TEBRA_USER or "")}</sch:User>
        </sch:RequestHeader>
        <sch:Fields>{fields_xml}
        </sch:Fields>
        <sch:Filter>
          <sch:EndDate>{end_date}T23:59:59</sch:EndDate>
          <sch:PracticeName>{escape(TEBRA_PRACTICE_NAME)}</sch:PracticeName>
          <sch:StartDate>{start_date}T00:00:00</sch:StartDate>
          <sch:TimeZoneOffsetFromGMT>0</sch:TimeZoneOffsetFromGMT>
        </sch:Filter>
      </sch:request>
    </sch:GetAppointments>
  </soapenv:Body>
</soapenv:Envelope>"""
    try:
        r = await _post_get_appointments_throttled(soap_body, rid)
    except Exception as e:
        logger.error("[%s] GetAppointments(practice) %s..%s exception=%s", rid, start_date, end_date, e)
        return {"ok": False, "error_message": f"Tebra request failed: {e}", "rows": []}
    xml = r.text
    logger.info("[%s] GetAppointments(practice) %s..%s status=%s len=%s", rid, start_date, end_date, r.status_code, len(xml))

    if r.status_code != 200:
        fault = parse_soap_fault(xml)
        return {"ok": False, "error_message": fault or f"HTTP {r.status_code}", "rows": []}
    fault = parse_soap_fault(xml)
    if fault:
        logger.error("[%s] GetAppointments(practice) fault=%s", rid, fault)
        return {"ok": False, "error_message": fault, "rows": []}
    is_error = re.search(r"<IsError>(true|false)</IsError>", xml)
    if is_error and is_error.group(1).lower() == "true":
        err = re.search(r"<ErrorMessage>([^<]*)</ErrorMessage>", xml)
        msg = (err.group(1).strip() if err else "") or "Tebra GetAppointments error"
        logger.error("[%s] GetAppointments(practice) IsError=%s", rid, msg)
        return {"ok": False, "error_message": msg, "rows": []}
    auth = re.search(r"<Authenticated>(true|false)</Authenticated>", xml)
    if auth and auth.group(1).lower() != "true":
        return {"ok": False, "error_message": "Not authenticated with Tebra", "rows": []}

    rows = []
    for b in re.findall(r"<AppointmentData>(.*?)</AppointmentData>", xml, re.DOTALL):
        f = {t: _extract_tag(b, t) for t in _PRACTICE_APPT_FIELDS}
        start = parse_tebra_datetime(f.get("StartDate"))
        if not f.get("ID") or start is None:
            continue
        start_utc = start.replace(tzinfo=timezone.utc)
        end = parse_tebra_datetime(f.get("EndDate"))
        try:
            duration = int(f.get("AppointmentDuration") or 0)
        except ValueError:
            duration = 0
        if end is not None and end > start:
            end_utc = end.replace(tzinfo=timezone.utc)
            duration = duration or int((end - start).total_seconds() // 60)
        else:
            duration = duration if duration > 0 else DEFAULT_SLOT_MINUTES
            end_utc = start_utc + timedelta(minutes=duration)
        resources = []
        for n in (1, 2):
            res_id, res_type = f.get(f"ResourceID{n}"), f.get(f"ResourceTypeID{n}")
            if res_id and res_id.isdigit():
                resources.append((int(res_id), int(res_type) if res_type and res_type.isdigit() else None))
        rows.append({
            "id":          f["ID"],
            "start_utc":   start_utc,
            "end_utc":     end_utc,
            "duration":    duration,
            "status":      f.get("ConfirmationStatus"),
            "type":        f.get("Type"),
            "location_id": f.get("ServiceLocationID"),
            "patient_id":  f.get("PatientID"),
            "reason_id":   f.get("AppointmentReasonID1"),
            "resources":   resources,
        })
    if len(rows) >= 10000:
        logger.warning("[%s] GetAppointments(practice) hit the 10,000-row cap for %s..%s — use a smaller range",
                       rid, start_date, end_date)
    return {"ok": True, "error_message": None, "rows": rows}


# ─── Patients for multi-provider booking (v2) ────────────────────────────────

# GetPatients answers IsError=true (no message) when called faster than ~1/second (seen 2026-10-30).
# The old get_patient_by_name treats that as "not found"; the v2 helpers throttle + retry instead.
_GET_PATIENTS_LOCK = asyncio.Lock()
_GET_PATIENTS_LAST = 0.0
_GET_PATIENTS_MIN_GAP = 1.1


def _phone_digits10(raw: str | None) -> str:
    digits = re.sub(r"[^\d]", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


async def find_patients_by_name(first_name: str, last_name: str, rid: str = "?") -> dict:
    """
    Every practice patient with this exact first + last name (PracticeID + PracticeName filters).
    Returns {"ok", "error", "patients": [{"id", "phones": [10-digit strings]}]}. Names are not returned
    (PII stays out of logs); callers match on phone.
    """
    global _GET_PATIENTS_LAST
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:GetPatients>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{escape(CUSTOMER_KEY or "")}</sch:CustomerKey>
          <sch:Password>{escape(TEBRA_PASSWORD or "")}</sch:Password>
          <sch:User>{escape(TEBRA_USER or "")}</sch:User>
        </sch:RequestHeader>
        <sch:Fields>
          <sch:HomePhone>true</sch:HomePhone>
          <sch:ID>true</sch:ID>
          <sch:MobilePhone>true</sch:MobilePhone>
          <sch:WorkPhone>true</sch:WorkPhone>
        </sch:Fields>
        <sch:Filter>
          <sch:FirstName>{escape((first_name or "").strip())}</sch:FirstName>
          <sch:LastName>{escape((last_name or "").strip())}</sch:LastName>
          <sch:PracticeID>{escape(str(PRACTICE_ID))}</sch:PracticeID>
          <sch:PracticeName>{escape(TEBRA_PRACTICE_NAME)}</sch:PracticeName>
        </sch:Filter>
      </sch:request>
    </sch:GetPatients>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetPatients",
    }
    xml, err = "", None
    async with _GET_PATIENTS_LOCK:
        for attempt in (1, 2, 3):
            wait = _GET_PATIENTS_MIN_GAP - (time.monotonic() - _GET_PATIENTS_LAST)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
                xml = r.text
            except Exception as e:
                xml, err = "", f"Tebra request failed: {e}"
            _GET_PATIENTS_LAST = time.monotonic()
            if not xml:
                logger.warning("[%s] GetPatients(v2) attempt=%s error=%s", rid, attempt, err)
                continue
            fault = parse_soap_fault(xml)
            if fault:
                return {"ok": False, "error": fault, "patients": []}
            is_error = re.search(r"<IsError>(true|false)</IsError>", xml)
            if is_error and is_error.group(1).lower() == "true":
                m = re.search(r"<ErrorMessage>([^<]*)</ErrorMessage>", xml)
                err = (m.group(1).strip() if m else "") or "GetPatients IsError (likely rate limit)"
                logger.warning("[%s] GetPatients(v2) attempt=%s IsError=%s — retrying", rid, attempt, err)
                await asyncio.sleep(1.5)
                continue
            err = None
            break
    if err:
        return {"ok": False, "error": err, "patients": []}

    patients = []
    for b in re.findall(r"<PatientData>(.*?)</PatientData>", xml, re.DOTALL):
        pid = _extract_tag(b, "ID")
        if not pid:
            continue
        phones = [p for p in (_phone_digits10(_extract_tag(b, t)) for t in ("MobilePhone", "HomePhone", "WorkPhone")) if p]
        patients.append({"id": pid, "phones": phones})
    if not patients:   # older response shapes: fall back to bare IDs
        patients = [{"id": i, "phones": []} for i in re.findall(r"<ID>(\d+)</ID>", xml)]
    logger.info("[%s] GetPatients(v2) matches=%s", rid, len(patients))
    return {"ok": True, "error": None, "patients": patients}


async def create_patient_v2(first_name: str, last_name: str, phone: str, gender: str | None = None,
                            rid: str = "?") -> dict:
    """
    CreatePatient with escaped values and an optional Gender (Male / Female; anything else = not sent).
    Returns {"ok", "patient_id", "error"}.
    """
    tebra_phone = _phone_digits10(phone)
    g = (gender or "").strip().lower()
    gender_xml = {"male": "\n          <sch:Gender>Male</sch:Gender>",
                  "female": "\n          <sch:Gender>Female</sch:Gender>"}.get(g, "")
    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:sch="http://www.kareo.com/api/schemas/">
  <soapenv:Header/>
  <soapenv:Body>
    <sch:CreatePatient>
      <sch:request>
        <sch:RequestHeader>
          <sch:CustomerKey>{escape(CUSTOMER_KEY or "")}</sch:CustomerKey>
          <sch:Password>{escape(TEBRA_PASSWORD or "")}</sch:Password>
          <sch:User>{escape(TEBRA_USER or "")}</sch:User>
        </sch:RequestHeader>
        <sch:Patient>
          <sch:FirstName>{escape(first_name.strip())}</sch:FirstName>{gender_xml}
          <sch:LastName>{escape(last_name.strip())}</sch:LastName>
          <sch:MobilePhone>{tebra_phone}</sch:MobilePhone>
          <sch:Practice>
            <sch:PracticeID>{escape(str(PRACTICE_ID))}</sch:PracticeID>
          </sch:Practice>
        </sch:Patient>
      </sch:request>
    </sch:CreatePatient>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/CreatePatient",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(TEBRA_URL, content=soap_body, headers=headers)
        xml = r.text
    except Exception as e:
        logger.error("[%s] CreatePatient(v2) exception=%s", rid, e)
        return {"ok": False, "patient_id": None, "error": f"Tebra request failed: {e}"}
    fault = parse_soap_fault(xml)
    if fault:
        logger.error("[%s] CreatePatient(v2) fault=%s", rid, fault)
        return {"ok": False, "patient_id": None, "error": fault}
    is_error = re.search(r"<IsError>(true|false)</IsError>", xml)
    if is_error and is_error.group(1).lower() == "true":
        m = re.search(r"<ErrorMessage>([^<]*)</ErrorMessage>", xml)
        err = (m.group(1).strip() if m else "") or "CreatePatient error"
        logger.error("[%s] CreatePatient(v2) error=%s", rid, err)
        return {"ok": False, "patient_id": None, "error": err}
    m = re.search(r"<PatientID>(\d+)</PatientID>", xml)
    if not m:
        logger.error("[%s] CreatePatient(v2) no PatientID in response (status=%s)", rid, r.status_code)
        return {"ok": False, "patient_id": None, "error": "PatientID not found in response"}
    logger.info("[%s] CreatePatient(v2) created patientId=%s gender=%s", rid, m.group(1), g or "-")
    return {"ok": True, "patient_id": m.group(1), "error": None}
