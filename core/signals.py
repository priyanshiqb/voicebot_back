"""
Core scheduling signals.

Kept intentionally thin: the AvailabilityException post_save handler does no
queries and no notifications — it only enqueues the reschedule task on commit,
so the heavy, idempotent work runs off the request path and never on a rolled-back
transaction.
"""

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from core.models.agent_call_log import AgentCallLog
from core.models.availability_exception import AvailabilityException
from core.models.booking_participant import BookingParticipant


@receiver(post_save, sender=AvailabilityException)
def enqueue_reschedule_on_leave(sender, instance, created, **kwargs):
    if not created or instance.is_deleted:
        return

    tenant_id = str(instance.tenant_id)
    exception_id = str(instance.id)

    def _enqueue():
        from core.tasks import reschedule_on_leave

        reschedule_on_leave.delay(tenant_id, exception_id)

    transaction.on_commit(_enqueue)


@receiver(pre_save, sender=AgentCallLog)
def link_call_log_to_contact(sender, instance, **kwargs):
    """
    Resolve caller_number to a Contact before the row is written.

    pre_save rather than post_save so linking costs no extra UPDATE, and it is a
    lookup on an indexed column. Resolution never fails a call log: an unknown or
    unparseable number simply leaves `contact` null.
    """
    if instance.contact_id is not None:
        return

    from core.services.contact_resolution import find_contact

    contact = find_contact(instance.tenant_id, instance.caller_number)
    if contact is not None:
        instance.contact = contact


@receiver(pre_save, sender=BookingParticipant)
def link_participant_to_contact(sender, instance, **kwargs):
    """Resolve a participant's phone_number to a Contact. Same contract as above."""
    if instance.contact_id is not None or not instance.phone_number:
        return

    from core.services.contact_resolution import find_contact

    tenant_id = getattr(instance, "booking", None) and instance.booking.tenant_id
    contact = find_contact(tenant_id, instance.phone_number)
    if contact is not None:
        instance.contact = contact
