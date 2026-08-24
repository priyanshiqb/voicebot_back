"""What a domain object costs, expressed as ``(operation, quantity)``.

Deliberately the only place that knows a call is billed per second of duration on its
service tier. Changing to a flat per-call price, or billing inbound differently, is an
edit here plus a rate row — never a change at the call sites.
"""

from core.constants.service_tier import ServiceTier

#: Fallback when neither the call log nor its agent names a tier. Standard rather than
#: realtime: guessing the most expensive tier would overcharge for a misconfiguration
#: that is not the tenant's fault.
DEFAULT_TIER = ServiceTier.CONVERSATION_HIGH_LATENCY


def tier_for_call(call_log):
    """The tier this call is billed at.

    The call log wins over the agent. A bot configured for low latency may have been
    served on the standard model — a capacity fallback, a degraded provider — and the
    tenant should pay for what actually ran, not what was requested. The agent's
    configured tier is the fallback for runners that do not report one yet.
    """
    tier = getattr(call_log, "service_tier", None)
    if tier:
        return tier

    agent = getattr(call_log, "agent", None)
    if agent is not None and getattr(agent, "service_tier", None):
        return agent.service_tier

    return DEFAULT_TIER


def operation_for_call(call_log):
    """``(operation, raw_quantity)`` for a completed call.

    Quantity is duration in **seconds**; the rate's unit decides how that becomes
    billable units. Returns ``None`` when the call carries no duration — the runner
    can post a row before it knows how long the call ran, and rating a zero-second
    call would either charge a spurious minimum or write a zero-credit ledger row that
    the non-zero constraint rejects. An unrated call is picked up by
    ``report_unrated_calls`` rather than guessed at.
    """
    duration = getattr(call_log, "duration_seconds", 0) or 0
    if duration <= 0:
        return None

    return tier_for_call(call_log), int(duration)
