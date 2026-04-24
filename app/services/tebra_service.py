import re
import httpx
from xml.sax.saxutils import escape

from app.core.config import (
    TEBRA_URL, CUSTOMER_KEY, TEBRA_PASSWORD, TEBRA_USER,
    PRACTICE_ID, PROVIDER_ID, RESOURCE_ID,
    TEBRA_TIMEZONE_OFFSET_FROM_GMT,
    TEBRA_INBOUND_APPOINTMENTS_PRACTICE_NAME,
)
from app.core.logger import logger
from app.utils.time_utils import parse_time_to_24hr, to_utc_string, parse_tebra_local_start_datetime, format_12hr
from app.utils.parser import parse_soap_fault


async def call_tebra_get_appointments(date: str, tebra_location_name: str) -> str:
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
          <sch:ID>true</sch:ID>
          <sch:PatientID>true</sch:PatientID>
          <sch:PatientFullName>true</sch:PatientFullName>
          <sch:StartDate>true</sch:StartDate>
          <sch:EndDate>true</sch:EndDate>
          <sch:AppointmentReason1>true</sch:AppointmentReason1>
          <sch:ConfirmationStatus>true</sch:ConfirmationStatus>
          <sch:ServiceLocationName>true</sch:ServiceLocationName>
        </sch:Fields>
        <sch:Filter>
          <sch:ServiceLocationName>{tebra_location_name}</sch:ServiceLocationName>
          <sch:StartDate>{date}T00:00:00</sch:StartDate>
          <sch:EndDate>{date}T23:59:59</sch:EndDate>
          <sch:TimeZoneOffsetFromGMT>{TEBRA_TIMEZONE_OFFSET_FROM_GMT}</sch:TimeZoneOffsetFromGMT>
        </sch:Filter>
      </sch:request>
    </sch:GetAppointments>
  </soapenv:Body>
</soapenv:Envelope>"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetAppointments",
    }
    logger.info("[tebra] GetAppointments date=%s location=%s", date, tebra_location_name)
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


def _parse_get_appointments_blocks(xml: str) -> list[dict]:
    blocks = re.findall(r"<AppointmentData>(.*?)</AppointmentData>", xml, re.DOTALL)
    out = []
    for b in blocks:
        row = {t: _extract_tag(b, t) for t in _APPT_DATA_TAGS}
        tid = row.get("ID")
        if not tid:
            continue
        parsed = None
        sd = row.get("StartDate")
        if sd:
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
    tz = timezone_offset_from_gmt if timezone_offset_from_gmt is not None else TEBRA_TIMEZONE_OFFSET_FROM_GMT
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
          <sch:ID>true</sch:ID>
          <sch:PatientID>true</sch:PatientID>
          <sch:PatientFullName>true</sch:PatientFullName>
          <sch:StartDate>true</sch:StartDate>
          <sch:EndDate>true</sch:EndDate>
          <sch:AppointmentDuration>true</sch:AppointmentDuration>
          <sch:AppointmentReason1>true</sch:AppointmentReason1>
          <sch:ConfirmationStatus>true</sch:ConfirmationStatus>
          <sch:ResourceName1>true</sch:ResourceName1>
          <sch:ServiceLocationName>true</sch:ServiceLocationName>
          <sch:Notes>true</sch:Notes>
          <sch:PracticeName>true</sch:PracticeName>
        </sch:Fields>
        <sch:Filter>
          <sch:PatientFullName>{safe_name}</sch:PatientFullName>
          <sch:StartDate>{start_date}</sch:StartDate>
          <sch:EndDate>{end_date}</sch:EndDate>
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

    appointments = _parse_get_appointments_blocks(xml)
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
    tz = timezone_offset_from_gmt if timezone_offset_from_gmt is not None else TEBRA_TIMEZONE_OFFSET_FROM_GMT
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
          <sch:ID>true</sch:ID>
          <sch:PatientID>true</sch:PatientID>
          <sch:PatientFullName>true</sch:PatientFullName>
          <sch:StartDate>true</sch:StartDate>
          <sch:EndDate>true</sch:EndDate>
          <sch:AppointmentDuration>true</sch:AppointmentDuration>
          <sch:AppointmentReason1>true</sch:AppointmentReason1>
          <sch:ConfirmationStatus>true</sch:ConfirmationStatus>
          <sch:ResourceName1>true</sch:ResourceName1>
          <sch:ServiceLocationName>true</sch:ServiceLocationName>
          <sch:Notes>true</sch:Notes>
          <sch:PracticeName>true</sch:PracticeName>
        </sch:Fields>
        <sch:Filter>
          <sch:PatientID>{pid}</sch:PatientID>
          <sch:PracticeName>{safe_practice}</sch:PracticeName>
          <sch:StartDate>{start_date}</sch:StartDate>
          <sch:EndDate>{end_date}</sch:EndDate>
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

    appointments = _parse_get_appointments_blocks(xml)
    return {"ok": True, "fault": None, "error_message": None, "appointments": appointments}


async def get_patient_by_name(first_name: str, last_name: str, rid: str = "?") -> str | None:
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
          <sch:ID>true</sch:ID>
          <sch:FirstName>true</sch:FirstName>
          <sch:LastName>true</sch:LastName>
          <sch:MobilePhone>true</sch:MobilePhone>
        </sch:Fields>
        <sch:Filter>
          <sch:FirstName>{escape((first_name or "").strip())}</sch:FirstName>
          <sch:LastName>{escape((last_name or "").strip())}</sch:LastName>
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
    rid: str = "?"
) -> dict:
    parsed = parse_time_to_24hr(time_str)
    if not parsed:
        return {"success": False, "appointment_id": None, "error": f"Could not parse time: {time_str}"}

    h, mn    = parsed
    start_dt = to_utc_string(date, h, mn)

    end_h, end_mn = h, mn + 30
    if end_mn >= 60:
        end_mn -= 60
        end_h  += 1
    end_dt = to_utc_string(date, end_h, end_mn)

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
          <sch:IsRecurring>false</sch:IsRecurring>
          <sch:PatientSummary>
            <sch:PatientId>{patient_id}</sch:PatientId>
          </sch:PatientSummary>
          <sch:PracticeId>{PRACTICE_ID}</sch:PracticeId>
          <sch:ProviderId>{PROVIDER_ID}</sch:ProviderId>
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

    logger.info("[%s] CreateAppointment patientId=%s locationId=%s reasonId=%s start=%s end=%s",
                rid, patient_id, location_id, appointment_reason_id, start_dt, end_dt)

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
