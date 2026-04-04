import re

from fastapi.responses import JSONResponse


def parse_soap_fault(xml: str) -> str | None:
    """Return the fault message string if the XML contains a SOAP fault, else None."""
    if "<s:Fault>" in xml or "<faultstring>" in xml:
        m = re.search(r'<faultstring[^>]*>([^<]+)</faultstring>', xml)
        return m.group(1) if m else "SOAP Fault received"
    return None


def build_vapi_response(tool_call_id: str | None, message: str):
    """Build the correct JSON response format for VAPI tool calls or direct HTTP tests."""
    if tool_call_id:
        return JSONResponse(content={
            "results": [{"toolCallId": tool_call_id, "result": message}]
        })
    return JSONResponse(content={"message": message})
