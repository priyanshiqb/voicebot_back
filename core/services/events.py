"""Append-only BookingEvent audit-log helper, shared by the booking, reschedule
and notification services (kept separate to avoid an import cycle)."""

from core.models.booking_event import BookingEvent


def log_event(
    booking,
    event_type,
    *,
    actor=None,
    from_host=None,
    to_host=None,
    from_host_id=None,
    to_host_id=None,
    metadata=None,
):
    """Record a single immutable BookingEvent row, tenant-scoped to the booking.
    Hosts may be passed as objects (from_host/to_host) or ids (from_host_id/to_host_id)."""
    kwargs = {
        "tenant": booking.tenant,
        "booking": booking,
        "event_type": event_type,
        "actor": actor,
        "metadata": metadata or {},
    }
    if from_host is not None:
        kwargs["from_host"] = from_host
    if from_host_id is not None:
        kwargs["from_host_id"] = from_host_id
    if to_host is not None:
        kwargs["to_host"] = to_host
    if to_host_id is not None:
        kwargs["to_host_id"] = to_host_id
    return BookingEvent.objects.create(**kwargs)
