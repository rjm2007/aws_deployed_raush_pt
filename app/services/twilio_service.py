import base64
import re
from typing import Optional

import httpx

from app.core.config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER


def _format_phone_e164(phone: str | None) -> str | None:
    """
    Best-effort E.164 formatter.
    - If the input already starts with '+', keep it (after stripping non-digits/+).
    - If it's 10 digits, assume US +1.
    """
    if not phone:
        return None
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    digits_only = cleaned.replace("+", "")
    if len(digits_only) < 10 or len(digits_only) > 15:
        return None
    if cleaned.startswith("+"):
        if len(digits_only) < 11:
            return None
        return cleaned
    if len(digits_only) == 10:
        return "+1" + digits_only
    if len(digits_only) == 11 and digits_only.startswith("1"):
        return "+" + digits_only
    return "+" + digits_only


async def twilio_send_sms(to_phone: str | None, body: str) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Sends an SMS via Twilio REST API.
    Returns: (ok, sid, error_message)
    """
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_FROM_NUMBER:
        return False, None, "Twilio is not configured (missing TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER)."

    to_e164 = _format_phone_e164(to_phone)
    if not to_e164:
        return False, None, f"Invalid destination phone number: {to_phone!r}"

    from_e164 = _format_phone_e164(TWILIO_FROM_NUMBER) or TWILIO_FROM_NUMBER

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    auth = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")).decode("utf-8")
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "To": to_e164,
        "From": from_e164,
        "Body": body,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, headers=headers, data=data)
            if r.status_code in (200, 201):
                sid = (r.json() or {}).get("sid")
                return True, sid, None
            return False, None, f"Twilio error status={r.status_code} body={r.text[:300]}"
    except Exception as e:
        return False, None, f"Twilio exception: {e}"

