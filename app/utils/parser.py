import json
import re

from fastapi.responses import JSONResponse


def parse_soap_fault(xml: str) -> str | None:
    """Return the fault message string if the XML contains a SOAP fault, else None."""
    if "<s:Fault>" in xml or "<faultstring>" in xml:
        m = re.search(r'<faultstring[^>]*>([^<]+)</faultstring>', xml)
        return m.group(1) if m else "SOAP Fault received"
    return None


def coerce_vapi_tool_arguments(raw) -> dict:
    """
    Vapi often sends OpenAI-style function.arguments as a JSON string; coerce to dict.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def vapi_response_content(tool_call_id: str | None, message: str) -> dict:
    """Dict sent to Vapi for a tool result (same shape as JSONResponse body)."""
    if isinstance(message, str):
        # Vapi rejects multi-line tool results; collapse whitespace to a single line.
        message = " ".join(message.split())
    if tool_call_id:
        return {"results": [{"toolCallId": tool_call_id, "result": message}]}
    return {"message": message}


def build_vapi_response(tool_call_id: str | None, message: str):
    """Build the correct JSON response format for VAPI tool calls or direct HTTP tests."""
    return JSONResponse(content=vapi_response_content(tool_call_id, message))


def extract_vapi_caller_number_from_body(body: dict | None) -> str | None:
    """
    Best-effort caller phone from a Vapi server/tool payload (voice: customer or call.from).
    Used for inbound create_appointment so SMS and Supabase use the number the patient called from.
    """
    if not isinstance(body, dict):
        return None
    msg = body.get("message") if isinstance(body.get("message"), dict) else {}
    cust = msg.get("customer") if isinstance(msg.get("customer"), dict) else {}
    call_obj = msg.get("call") if isinstance(msg.get("call"), dict) else {}
    raw = (
        cust.get("number")
        or cust.get("phoneNumber")
        or call_obj.get("from")
        or call_obj.get("fromNumber")
        or (call_obj.get("customer", {}) or {}).get("number")
    )
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None
