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


def build_vapi_response(tool_call_id: str | None, message: str):
    """Build the correct JSON response format for VAPI tool calls or direct HTTP tests."""
    if isinstance(message, str):
        # Vapi rejects multi-line tool results; collapse whitespace to a single line.
        message = " ".join(message.split())
    if tool_call_id:
        return JSONResponse(content={
            "results": [{"toolCallId": tool_call_id, "result": message}]
        })
    return JSONResponse(content={"message": message})
