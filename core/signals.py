"""
Core scheduling signals.

Kept intentionally thin: the AvailabilityException post_save handler does no
queries and no notifications — it only enqueues the reschedule task on commit,
so the heavy, idempotent work runs off the request path and never on a rolled-back
transaction.
"""

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.models.availability_exception import AvailabilityException


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
