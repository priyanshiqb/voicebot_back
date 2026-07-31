"""
LLM tool schemas + dispatcher for the AI-agent scheduling bridge.

`TOOL_SCHEMAS` is a provider-agnostic list of function-calling tool definitions
(Anthropic `tools` shape: name / description / input_schema — trivially adaptable to
the OpenAI `functions` shape). The **agent is NOT a tool argument** — it is the call's
context and is bound by `call_tool(agent, name, arguments)`, which also parses date /
datetime strings and routes to the guardrailed `agent_scheduling` service.

Every tool returns the service's structured result: `{"ok": True, ...}` or
`{"ok": False, "reason": "...", ...}` — safe to narrate directly to the caller.
"""

from datetime import date, datetime
from datetime import timezone as dt_timezone

from core.services import agent_scheduling

_PARTICIPANT_SCHEMA = {
    "type": "object",
    "description": "The caller being booked. Capture at least a name; an email enables a confirmation.",
    "properties": {
        "name": {"type": "string", "description": "Caller's full name."},
        "email": {"type": "string", "description": "Caller's email (for confirmation/reminders)."},
        "phone": {"type": "string", "description": "Caller's phone number, if given."},
    },
}

_EVENT_TEMPLATE_ID = {
    "type": "string",
    "description": "Id of the meeting type, from list_event_types.",
}
_TARGET_HOST_ID = {
    "type": "string",
    "description": (
        "The host to book. REQUIRED for every meeting type except collective ones — "
        "nothing is auto-assigned. Get an id from list_event_hosts, or use one of the "
        "host_ids returned with the slot you picked in check_availability. Omit ONLY "
        "for a collective meeting, which books every host."
    ),
}
_START_TIME = {
    "type": "string",
    "description": "Chosen slot start in ISO-8601 UTC, e.g. 2027-03-16T10:00:00Z (use a start returned by check_availability).",
}

TOOL_SCHEMAS = [
    {
        "name": "list_event_types",
        "description": (
            "List the meeting types this agent is allowed to book. Call this first "
            "when you don't already know which event type the caller wants."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_event_hosts",
        "description": (
            "List the people who can take a meeting type, with the host ids needed to "
            "book it. Call this when the caller names a person, or when you need a "
            "target_host_id and don't have one from check_availability."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"event_template_id": _EVENT_TEMPLATE_ID},
            "required": ["event_template_id"],
        },
    },
    {
        "name": "check_availability",
        "description": (
            "Return open time slots for a meeting type between two dates (inclusive). "
            "Offer the returned start times to the caller. Each slot lists the host_ids "
            "free at that time — carry one of them into book_meeting/hold_slot as "
            "target_host_id. Pass target_host_id here to check one host's times only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_template_id": _EVENT_TEMPLATE_ID,
                "date_from": {"type": "string", "description": "Search window start, YYYY-MM-DD."},
                "date_to": {"type": "string", "description": "Search window end (inclusive), YYYY-MM-DD."},
                "target_host_id": _TARGET_HOST_ID,
            },
            "required": ["event_template_id", "date_from", "date_to"],
        },
    },
    {
        "name": "hold_slot",
        "description": (
            "Temporarily reserve a slot while you confirm details on the call, so no "
            "one else can take it. Always follow with confirm_booking. Requires "
            "target_host_id. Not available for collective meetings (use book_meeting)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_template_id": _EVENT_TEMPLATE_ID,
                "start_time": _START_TIME,
                "participant": _PARTICIPANT_SCHEMA,
                "target_host_id": _TARGET_HOST_ID,
            },
            "required": ["event_template_id", "start_time"],
        },
    },
    {
        "name": "confirm_booking",
        "description": "Confirm a previously held slot (from hold_slot) into a real booking.",
        "input_schema": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "Id returned by hold_slot."}
            },
            "required": ["booking_id"],
        },
    },
    {
        "name": "book_meeting",
        "description": (
            "Book a meeting in one step (no prior hold). Requires target_host_id, "
            "except for a collective meeting, which books every required host together."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_template_id": _EVENT_TEMPLATE_ID,
                "start_time": _START_TIME,
                "participant": _PARTICIPANT_SCHEMA,
                "target_host_id": _TARGET_HOST_ID,
            },
            "required": ["event_template_id", "start_time"],
        },
    },
    {
        "name": "cancel_booking",
        "description": "Cancel a booking (or the whole group, for a collective meeting).",
        "input_schema": {
            "type": "object",
            "properties": {
                "booking_id": {"type": "string", "description": "Id of the booking to cancel."},
                "reason": {"type": "string", "description": "Optional cancellation reason."},
            },
            "required": ["booking_id"],
        },
    },
]


# --- argument parsing --------------------------------------------------------

def _parse_dt(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt


def _parse_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


# --- dispatch ----------------------------------------------------------------

_DISPATCH = {
    "list_event_types": lambda agent, a: agent_scheduling.list_bookable_event_templates(agent),
    "list_event_hosts": lambda agent, a: agent_scheduling.list_event_hosts(
        agent, a["event_template_id"]
    ),
    "check_availability": lambda agent, a: agent_scheduling.check_availability(
        agent,
        a["event_template_id"],
        _parse_date(a["date_from"]),
        _parse_date(a["date_to"]),
        target_host_id=a.get("target_host_id"),
    ),
    "hold_slot": lambda agent, a: agent_scheduling.hold(
        agent,
        a["event_template_id"],
        _parse_dt(a["start_time"]),
        participant=a.get("participant"),
        target_host_id=a.get("target_host_id"),
    ),
    "confirm_booking": lambda agent, a: agent_scheduling.confirm(agent, a["booking_id"]),
    "book_meeting": lambda agent, a: agent_scheduling.book(
        agent,
        a["event_template_id"],
        _parse_dt(a["start_time"]),
        participant=a.get("participant"),
        target_host_id=a.get("target_host_id"),
    ),
    "cancel_booking": lambda agent, a: agent_scheduling.cancel(
        agent, a["booking_id"], reason=a.get("reason", "")
    ),
}


def call_tool(agent, name, arguments=None):
    """Execute a scheduling tool for `agent`. Returns the service's structured result,
    or `{"ok": False, "reason": "unknown_tool"|"bad_arguments"}` on a dispatch/arg error."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"ok": False, "reason": "unknown_tool", "message": name}
    try:
        return fn(agent, arguments or {})
    except (KeyError, ValueError, TypeError) as exc:
        return {"ok": False, "reason": "bad_arguments", "message": str(exc)}
