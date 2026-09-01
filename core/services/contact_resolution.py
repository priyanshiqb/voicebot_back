"""
Resolve free-text phone numbers on call logs and booking participants to Contacts.

Both models carry a bare phone string. Matching on that string would fail the moment
someone writes "+1 (415) 555-2671" instead of "+14155552671", so resolution goes
through the same E.164 normalization the Contact model uses.
"""

import logging

from core.models.contact import Contact
from utils.phone import PhoneNormalizationError, normalize_phone
from utils.querysets import batched_queryset

logger = logging.getLogger(__name__)


def find_contact(tenant_id, phone_number, region=None):
    """
    Return the Contact for `phone_number` within a tenant, or None.

    Never raises: an unparseable number simply means no match. Resolution is a
    convenience on top of call/booking data, not a gate on it.
    """
    if not tenant_id or not phone_number:
        return None

    try:
        e164, _region = normalize_phone(phone_number, region=region)
    except PhoneNormalizationError:
        return None

    return Contact.objects.filter(tenant_id=tenant_id, phone_e164=e164).first()


def resolve_call_log_contact(call_log, save=True):
    """Attach the matching Contact to a call log, if one exists.

    Resolved from `customer_number`, not `caller_number`: on an outbound call the
    caller is the bot, and matching on it would either find nothing or — worse — find
    whichever Contact happens to hold the bot's own number.
    """
    contact = find_contact(call_log.tenant_id, call_log.customer_number)
    if contact is None or call_log.contact_id == contact.id:
        return call_log.contact

    call_log.contact = contact
    if save:
        call_log.save(update_fields=["contact", "updated_at"])
    return contact


def resolve_participant_contact(participant, save=True):
    """Attach the matching Contact to a booking participant, if one exists."""
    tenant_id = getattr(participant.booking, "tenant_id", None)
    contact = find_contact(tenant_id, participant.phone_number)
    if contact is None or participant.contact_id == contact.id:
        return participant.contact

    participant.contact = contact
    if save:
        participant.save(update_fields=["contact", "updated_at"])
    return contact


def backfill_call_logs(tenant_id=None, batch_size=500):
    """
    Populate `contact` on existing call logs.

    Kept out of the schema migration on purpose — a slow backfill must not block a
    deploy. Run from the management command instead.
    """
    from core.models.agent_call_log import AgentCallLog

    queryset = AgentCallLog.objects.filter(contact__isnull=True)
    if tenant_id:
        queryset = queryset.filter(tenant_id=tenant_id)

    linked = 0
    updates = []
    for call_log in batched_queryset(queryset, batch_size=batch_size):
        # customer_number, so outbound rows resolve against the person rather than
        # the bot. Old outbound rows fall back to caller_number and keep working.
        contact = find_contact(call_log.tenant_id, call_log.customer_number)
        if contact is None:
            continue
        call_log.contact = contact
        updates.append(call_log)

        if len(updates) >= batch_size:
            AgentCallLog.objects.bulk_update(updates, ["contact"])
            linked += len(updates)
            updates = []

    if updates:
        AgentCallLog.objects.bulk_update(updates, ["contact"])
        linked += len(updates)

    return linked


def backfill_participants(tenant_id=None, batch_size=500):
    """Populate `contact` on existing booking participants."""
    from core.models.booking_participant import BookingParticipant

    queryset = BookingParticipant.objects.filter(
        contact__isnull=True, phone_number__isnull=False
    ).select_related("booking")
    if tenant_id:
        queryset = queryset.filter(booking__tenant_id=tenant_id)

    linked = 0
    updates = []
    for participant in batched_queryset(queryset, batch_size=batch_size):
        contact = find_contact(
            getattr(participant.booking, "tenant_id", None), participant.phone_number
        )
        if contact is None:
            continue
        participant.contact = contact
        updates.append(participant)

        if len(updates) >= batch_size:
            BookingParticipant.objects.bulk_update(updates, ["contact"])
            linked += len(updates)
            updates = []

    if updates:
        BookingParticipant.objects.bulk_update(updates, ["contact"])
        linked += len(updates)

    return linked
