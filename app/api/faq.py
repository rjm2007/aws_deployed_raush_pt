import time
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.logger import logger
from app.services.faq_service import search_faq
from app.utils.parser import build_vapi_response, coerce_vapi_tool_arguments

router = APIRouter(tags=["FAQ"])


@router.post("/faq-search", summary="Clinic FAQ lookup (Vapi tool `clinic_faq_search`, or direct JSON {question})")
async def faq_search(request: Request):
    rid = uuid4().hex[:8]
    t0 = time.perf_counter()
    body = await request.json()
    tool_call_id = None
    if isinstance(body, dict) and "message" in body and body["message"].get("toolCalls"):
        tc = body["message"]["toolCalls"][0]
        tool_call_id = tc.get("id")
        args = coerce_vapi_tool_arguments(tc.get("function", {}).get("arguments"))
    else:
        args = body if isinstance(body, dict) else {}
    question = str(args.get("question") or "").strip()

    hits = await search_faq(question) if question else []
    if not hits:
        message = "No matching clinic information. Tell the caller you're not sure and that the team will follow up."
    else:
        parts = []
        for h in hits:
            tag = " [FOLLOW-UP: give this short answer, then say the team will follow up]" if h.get("follow_up") else ""
            parts.append(f"Q: {h['question']}{tag} A: {h['answer']}")
        message = "Clinic facts (answer only from these, briefly): " + " | ".join(parts)
    logger.info("[%s] faq-search q=%r hits=%s ms=%.0f", rid, question[:120],
                [h["question"][:40] for h in hits], (time.perf_counter() - t0) * 1000)
    return build_vapi_response(tool_call_id, message)


@router.post("/kb/search", summary="Vapi custom knowledge base endpoint (knowledge-base-request → documents)")
async def custom_kb_search(request: Request):
    rid = uuid4().hex[:8]
    t0 = time.perf_counter()
    body = await request.json()
    msg = (body or {}).get("message") or {}
    if msg.get("type") != "knowledge-base-request":
        return JSONResponse(status_code=400, content={"error": "Invalid request type"})
    users = [m.get("content") or "" for m in msg.get("messages", []) if m.get("role") == "user"]
    query = users[-1] if users else ""
    hits = await search_faq(query)
    docs = [{"content": f"{h['question']}: {h['answer']}" + (" [FOLLOW-UP]" if h.get("follow_up") else ""),
             "similarity": h["score"], "uuid": f"faq-{h['id']}"} for h in hits]
    logger.info("[%s] kb/search q=%r docs=%d ms=%.0f", rid, query[:120], len(docs), (time.perf_counter() - t0) * 1000)
    return {"documents": docs}
