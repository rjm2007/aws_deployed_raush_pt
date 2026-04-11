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


def _caller_number_from_customer_dict(obj: dict | None) -> str | None:
    if not isinstance(obj, dict):
        return None
    raw = obj.get("number") or obj.get("phoneNumber")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def extract_vapi_caller_number_from_body(body: dict | None) -> str | None:
    """
    Best-effort caller phone from a Vapi server/tool payload.

    Typical inbound voice (function / server webhook): message.call.customer.number.
    apiRequest tools often POST a flat JSON body (no message wrapper); if Vapi merges
    static parameters (e.g. phone from {{ customer.number }}), that appears as top-level
    fields. Also checks variableValues/variables.customer when present.
    """
    if not isinstance(body, dict):
        return None
    msg = body.get("message") if isinstance(body.get("message"), dict) else {}
    call_obj = msg.get("call") if isinstance(msg.get("call"), dict) else {}
    top_call = body.get("call") if isinstance(body.get("call"), dict) else {}

    for cust in (
        call_obj.get("customer") if isinstance(call_obj.get("customer"), dict) else None,
        msg.get("customer") if isinstance(msg.get("customer"), dict) else None,
        top_call.get("customer") if isinstance(top_call.get("customer"), dict) else None,
        body.get("customer") if isinstance(body.get("customer"), dict) else None,
    ):
        n = _caller_number_from_customer_dict(cust)
        if n:
            return n

    for vv_key in ("variableValues", "variables"):
        vv = body.get(vv_key)
        if isinstance(vv, dict):
            cust = vv.get("customer")
            n = _caller_number_from_customer_dict(cust if isinstance(cust, dict) else None)
            if n:
                return n

    raw = (
        call_obj.get("from")
        or call_obj.get("fromNumber")
        or top_call.get("from")
        or top_call.get("fromNumber")
    )
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None
