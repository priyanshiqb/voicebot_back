"""
LLM tool schemas + dispatcher for the AI-agent scheduling bridge.

`TOOL_SCHEMAS` is a provider-agnostic list of function-calling tool definitions
(Anthropic `tools` shape: name / description / input_schema — trivially adaptable to
the OpenAI `functions` shape). The **agent is NOT a tool argument** — it is the call's
context and is bound by `call_tool(agent, name, arguments)`, which also parses date /
datetime strings and routes to the guardrailed `agent_scheduling` service.

Every tool returns the service's structured result: `{"ok": True, ...}` or
`{"ok": False, "reason": "...", ...}` — safe to narrate directly to the caller.

**Timezone contract.** The agent thinks in two clocks and must never mix them:

  * `start` / `end` / `hold_expires_at` are **UTC**. They exist to be passed back
    into another tool call, never to be spoken.
  * `speak` and `start_local` are the **caller's** clock (`agent.timezone`).
    `speak` is preformatted for exactly this reason — an LLM asked to convert a
    UTC timestamp mid-call will sometimes get the offset wrong, and on a voice
    call a wrong time sounds identical to a right one.

Dates going in (`date_from`/`date_to`) are the caller's dates. Datetimes going in
without an offset are read as the caller's wall clock; with an offset, honoured
as given. See `agent_scheduling.conversation_zone`.
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
    "description": (
        "Chosen slot start. STRONGLY PREFER copying the `start` value from the "
        "check_availability slot the caller picked, exactly as given (ISO-8601 UTC, "
        "e.g. 2027-03-16T10:00:00Z). If you must construct a time yourself, write it "
        "in the caller's local wall-clock with NO timezone suffix (e.g. "
        "2027-03-16T15:00:00) and it will be read in the agent's timezone. Never "
        "convert a time to UTC yourself."
    ),
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
            "Dates are the CALLER'S dates — 'Tuesday' means Tuesday where the caller "
            "is, and that is how they are interpreted. "
            "Read each slot's `speak` field aloud; it is already in the caller's "
            "timezone. Do NOT read `start` aloud — it is UTC, and is only for passing "
            "back to book_meeting/hold_slot. Each slot lists the host_ids free at that "
            "time — carry one into book_meeting/hold_slot as target_host_id. Pass "
            "target_host_id here to check one host's times only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_template_id": _EVENT_TEMPLATE_ID,
                "date_from": {
                    "type": "string",
                    "description": "Search window start, YYYY-MM-DD, in the caller's local calendar.",
                },
                "date_to": {
                    "type": "string",
                    "description": "Search window end (inclusive), YYYY-MM-DD, in the caller's local calendar.",
                },
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

def _parse_dt(value, agent):
    """Parse a datetime the LLM produced, into an aware UTC instant.

    A value WITHOUT an offset is read in the agent's SPOKEN timezone, not UTC.
    This matters: models routinely echo back "2027-03-16T15:00:00" after a caller
    says "three o'clock". Reading that as UTC books 15:00Z — which for an agent
    speaking IST is 20:30 to the caller, five and a half hours from what they
    agreed to, with a confirmation email that looks perfectly correct.

    An explicit offset (including `Z`) is always honoured as given, so passing a
    `start` straight back from check_availability stays exact.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=agent_scheduling.conversation_zone(agent))
    return dt.astimezone(dt_timezone.utc)


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
        _parse_dt(a["start_time"], agent),
        participant=a.get("participant"),
        target_host_id=a.get("target_host_id"),
    ),
    "confirm_booking": lambda agent, a: agent_scheduling.confirm(agent, a["booking_id"]),
    "book_meeting": lambda agent, a: agent_scheduling.book(
        agent,
        a["event_template_id"],
        _parse_dt(a["start_time"], agent),
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
