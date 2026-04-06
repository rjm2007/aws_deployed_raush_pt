import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import resolve_location
from app.core.logger import logger
from app.models.requests import CheckAvailabilityRequest, inline_schema_refs
from app.services.tebra_service import call_tebra_get_appointments
from app.utils.time_utils import (
    parse_booked_slots,
    get_available_slots,
    get_free_ranges,
    parse_time_to_24hr,
    is_valid_clinic_slot,
    format_12hr,
    get_nearest_available_slots,
)
from app.utils.parser import build_vapi_response

router = APIRouter(tags=["Availability"])


@router.post(
    "/check-availability",
    summary="Check available appointment slots for a given date and location",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": inline_schema_refs(CheckAvailabilityRequest.model_json_schema())}},
            "required": True,
        }
    },
)
async def check_availability(request: Request):
    try:
        rid  = str(uuid4())[:8]
        body = await request.json()
        logger.info("[%s] ======== check-availability START ========", rid)
        logger.info("[%s] check-availability body=%s", rid, body)

        tool_call_id = None
        if "message" in body and "toolCalls" in body["message"]:
            tc           = body["message"]["toolCalls"][0]
            tool_call_id = tc.get("id")
            args         = tc["function"]["arguments"]
            date         = args.get("date")
            time_str     = args.get("time")
            service      = args.get("service")
            location     = args.get("location")
        else:
            date     = body.get("date")
            time_str = body.get("time")
            service  = body.get("service")
            location = body.get("location")

        loc        = resolve_location(location)
        tebra_name = loc["name"]
        logger.info("[%s] check-availability date=%s time=%s location=%s tebraName=%s",
                    rid, date, time_str, location, tebra_name)

        # ── Validate date format ──
        if not date or not re.match(r'^\d{4}-\d{2}-\d{2}$', str(date)):
            message = (
                f"I need the date in YYYY-MM-DD format, for example 2026-04-02. "
                f"You said '{date}' — could you give me the exact date?"
            )
            logger.warning("[%s] check-availability invalid date format: %s", rid, date)
            return build_vapi_response(tool_call_id, message)

        # ── Block Sundays — clinic is closed ──
        requested_date = datetime.strptime(date, "%Y-%m-%d")
        if requested_date.weekday() == 6:  # Sunday
            logger.info("[%s] check-availability BLOCKED — %s is a Sunday", rid, date)
            return build_vapi_response(
                tool_call_id,
                f"Sorry, {date} is a Sunday and the clinic is closed. "
                f"We are open Monday through Saturday. Would you like to check another date?"
            )

        xml_response = await call_tebra_get_appointments(date, tebra_name)

        # ── Detect Tebra-level error ──
        if "<IsError>true</IsError>" in xml_response:
            err_m     = re.search(r'<ErrorMessage>([^<]+)</ErrorMessage>', xml_response)
            tebra_err = err_m.group(1) if err_m else "Unknown Tebra error"
            logger.error("[%s] check-availability tebra_error=%s", rid, tebra_err)
            return build_vapi_response(
                tool_call_id,
                "I couldn't check availability for that date. Please give me the date in YYYY-MM-DD format, for example 2026-04-02."
            )

        booked    = parse_booked_slots(xml_response, date)
        available = get_available_slots(booked)

        # ── Filter out past slots when date is today (PDT) ──
        pdt_now = datetime.now(timezone(timedelta(hours=-7)))
        if date == pdt_now.strftime("%Y-%m-%d"):
            available = [
                (h, mn) for (h, mn) in available
                if (h * 60 + mn) > (pdt_now.hour * 60 + pdt_now.minute)
            ]

        requested_time = parse_time_to_24hr(time_str) if time_str else None

        if requested_time is None:
            if not available:
                message = f"Sorry, there are no available slots on {date} at {location}."
            else:
                ranges_str = ", then ".join(get_free_ranges(available))
                message = (
                    f"Available slots on {date} at {location}: {ranges_str} "
                    f"— all in 30-minute intervals."
                )
        else:
            h, mn = requested_time
            if not is_valid_clinic_slot(h, mn):
                message = (
                    f"Sorry, {format_12hr(h, mn)} is outside clinic hours. "
                    f"We're open 7:00 AM to 2:00 PM and 3:00 PM to 5:30 PM."
                )
            elif (h, mn) not in booked:
                message = (
                    f"Yes, {format_12hr(h, mn)} is available at {location} on {date}. "
                    f"Shall I go ahead and book it?"
                )
            elif not available:
                message = (
                    f"Sorry, {format_12hr(h, mn)} is not available at {location} on {date} "
                    f"and there are no other open slots."
                )
            else:
                nearest   = get_nearest_available_slots(h, mn, available)
                slots_str = ", ".join(nearest) if nearest else "no slots"
                message   = (
                    f"Sorry, {format_12hr(h, mn)} is already taken at {location}. "
                    f"The nearest open slots are: {slots_str}. "
                    f"Which of these works for you?"
                )

        logger.info("[%s] check-availability message=%s", rid, message)

        if not tool_call_id:
            return JSONResponse(content={
                "message":         message,
                "date":            date,
                "location":        location,
                "service":         service,
                "booked_count":    len(booked),
                "available_count": len(available),
            })
        return build_vapi_response(tool_call_id, message)

    except Exception as e:
        logger.exception("Error in /check-availability: %s", e)
        return build_vapi_response(
            locals().get("tool_call_id"),
            "Sorry, there was an error checking availability. Please try again."
        )
