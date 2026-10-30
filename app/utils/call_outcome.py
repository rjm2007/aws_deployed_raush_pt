"""Pure decision logic for the Vapi structured output "rausch_outbound_call_outcome" (post-call backup).

The webhook calls `extract_call_outcome()` to find our result in an end-of-call report, then
`decide_lead_update()` to turn it into a lead patch. Tools stay the source of truth:
- lead still in_progress/new (no tool saved anything) → apply the outcome when confidence ≥ 0.7
- lead already updated by a tool → never change status; only a do-not-call request (confidence ≥ 0.8)
  is flagged in metadata (+ lead_outcome not_interested unless the lead is booked)
Nothing here ever books or cancels.
"""

STRUCTURED_OUTPUT_NAME = "rausch_outbound_call_outcome"
APPLY_MIN_CONFIDENCE = 0.7
DNC_MIN_CONFIDENCE = 0.8

_TERMINAL = ("complete", "not_interested", "follow_up", "manual_follow_up")
_FALLBACK_OUTCOMES = ("voicemail", "someone_else_answered", "callback_requested",
                      "hung_up_mid_booking", "transfer_requested", "other", "no_conversation", "booked")


def extract_call_outcome(report: dict) -> dict | None:
    """Our structured output result from an end-of-call report (message.artifact.structuredOutputs), or None."""
    outputs = ((report or {}).get("artifact") or {}).get("structuredOutputs") \
        or ((report or {}).get("call") or {}).get("artifact", {}).get("structuredOutputs") or {}
    if not isinstance(outputs, dict):
        return None
    for entry in outputs.values():
        if isinstance(entry, dict) and entry.get("name") == STRUCTURED_OUTPUT_NAME:
            result = entry.get("result")
            return result if isinstance(result, dict) and result.get("outcome") else None
    return None


def _confidence(so: dict) -> float:
    try:
        return float(so.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def _note(so: dict) -> str:
    reason = (so.get("reason") or "").strip()
    cb = (so.get("callback_time_text") or "").strip()
    text = f"[call-outcome {so.get('outcome')}] {reason}".strip()
    if cb:
        text += f" Callback time said: {cb}."
    return text[:500]


def decide_lead_update(lead: dict, so: dict | None) -> dict:
    """Return {"mode", "data", "metadata", "note", "mismatch"}.

    mode:
      "none"     – no usable structured output (caller keeps current behaviour)
      "apply"    – lead untouched by tools: data/metadata replace the fallback status update
      "fallback" – lead untouched by tools but outcome isn't one we apply: keep fallback, add note
      "flag"     – lead already updated by tools: only metadata/outcome flag for do-not-call
      "log"      – lead already updated by tools: nothing to change (maybe a mismatch to log)
    """
    out = {"mode": "none", "data": {}, "metadata": {}, "note": None, "mismatch": None}
    if not so or not so.get("outcome"):
        return out
    outcome = so["outcome"]
    conf = _confidence(so)
    dnc = bool(so.get("do_not_call_requested")) or outcome == "do_not_call"
    qs = (lead or {}).get("queue_status")
    lead_outcome = (lead or {}).get("lead_outcome")
    note = _note(so)
    out["note"] = note

    if qs in _TERMINAL:
        if dnc and conf >= DNC_MIN_CONFIDENCE and not ((lead or {}).get("metadata") or {}).get("do_not_call"):
            out["mode"] = "flag"
            out["metadata"] = {"do_not_call": True, "do_not_call_source": "call_outcome"}
            if lead_outcome != "booked":
                out["data"] = {"lead_outcome": "not_interested", "queue_status": "not_interested"}
        else:
            out["mode"] = "log"
        expected = {"booked": "booked", "not_interested": "not_interested", "do_not_call": "not_interested"}
        if outcome in expected and lead_outcome and lead_outcome != expected[outcome]:
            out["mismatch"] = f"extraction={outcome} lead_outcome={lead_outcome}"
        elif lead_outcome == "booked" and outcome != "booked":
            out["mismatch"] = f"extraction={outcome} lead_outcome=booked"
        return out

    # Lead still in_progress/new: the agent's tools never saved a result.
    if conf < APPLY_MIN_CONFIDENCE:
        out["mode"] = "fallback"
        return out
    if dnc:
        out["mode"] = "apply"
        out["data"] = {"queue_status": "not_interested", "lead_outcome": "not_interested"}
        out["metadata"] = {"do_not_call": True, "do_not_call_source": "call_outcome"}
    elif outcome == "not_interested":
        out["mode"] = "apply"
        out["data"] = {"queue_status": "not_interested", "lead_outcome": "not_interested"}
    elif outcome == "wrong_number":
        out["mode"] = "apply"
        out["data"] = {"queue_status": "manual_follow_up", "lead_outcome": "manual"}
        out["metadata"] = {"wrong_number": True}
    else:
        # voicemail / someone else / callback / hang-up / transfer / other: current fallback + reason in notes
        out["mode"] = "fallback"
    return out
