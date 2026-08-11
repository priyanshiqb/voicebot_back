"""
Core scheduling signals.

Kept intentionally thin: the AvailabilityException post_save handler does no
queries and no notifications — it only enqueues the reschedule task on commit,
so the heavy, idempotent work runs off the request path and never on a rolled-back
transaction.

The CustomUser timezone pair is a signal rather than serializer logic on purpose:
a user's timezone is written from the users API, the admin, onboarding and the
shell, and a host whose working hours silently stopped following them because the
change came in through the wrong door is not a bug anyone would find quickly.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.signals import m2m_changed, post_save, pre_save
from django.dispatch import receiver

from core.models.agent_call_log import AgentCallLog
from core.models.availability_exception import AvailabilityException
from core.models.booking_participant import BookingParticipant
from core.models.catalog_item import CatalogItem
from user.models import CustomUser

# Stashed on the instance between pre_save and post_save. An attribute rather than
# a module-level dict because concurrent requests share the module but not the
# instance.
_PREVIOUS_TIMEZONE_ATTR = "_previous_timezone_id"


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


@receiver(pre_save, sender=CustomUser)
def remember_previous_timezone(sender, instance, **kwargs):
    """Capture the timezone about to be overwritten.

    ``post_save`` can see the new value but not the old one, and the old one is
    what decides whether the user's zone and their profile's were in step. Stashed
    on the instance so the read happens once, on the save that matters.
    """
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "timezone" not in update_fields:
        # Saves that name their fields and do not touch timezone — `last_login`,
        # OTP stamps — skip the lookup entirely. This runs on every user write.
        setattr(instance, _PREVIOUS_TIMEZONE_ATTR, None)
        return
    if instance.pk is None:
        setattr(instance, _PREVIOUS_TIMEZONE_ATTR, None)
        return

    previous = (
        sender.all_objects.filter(pk=instance.pk)
        .values_list("timezone_id", flat=True)
        .first()
    )
    setattr(instance, _PREVIOUS_TIMEZONE_ATTR, previous)


@receiver(post_save, sender=CustomUser)
def sync_availability_profile_timezone(sender, instance, created, **kwargs):
    """Move the host's availability profile to their new timezone.

    A relocating host means "9 to 5" now, not "9 to 5 where I used to live". The
    wall-clock rules are untouched; only the zone they resolve against changes, and
    the profile is re-materialized so future slots follow.

    Existing bookings are unaffected — they are absolute instants, already agreed
    with a customer, and moving them because the host changed a preference would be
    a very unwelcome surprise.
    """
    if created:
        return
    previous = getattr(instance, _PREVIOUS_TIMEZONE_ATTR, None)
    if previous is None or previous == instance.timezone_id:
        return

    from core.services.profile_sync import sync_profile_timezone

    sync_profile_timezone(instance, previous)


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


@receiver(m2m_changed, sender=CatalogItem.add_on_items.through)
def validate_catalog_add_ons(sender, instance, action, reverse, pk_set, **kwargs):
    """Refuse an add-on link that is cross-tenant, self-referential, or not an add-on.

    A signal rather than `CatalogItem.clean()` because `clean()` cannot see this at
    all: M2M rows are written after the instance is saved, so `full_clean()` on a new
    item validates an empty relation. This receiver is the only hook that fires for
    every path — `item.add_on_items.add(x)`, `.set([...])`, the admin's
    `filter_horizontal` widget, a serializer, and the shell alike.

    The serializer should still validate, so the API returns a field error rather than
    a 500. This is the backstop that makes the rule true regardless.
    """
    if action not in ("pre_add", "pre_remove") or not pk_set:
        return
    if action == "pre_remove":
        return

    # `reverse` means someone wrote `add_on.add_on_for.add(item)` — the roles are
    # swapped, and the thing being validated as an add-on is `instance`, not the set.
    if reverse:
        if instance.item_kind != CatalogItem.ItemKind.ADD_ON:
            raise ValidationError(
                f"'{instance.name}' is not an add-on (item_kind is "
                f"{instance.item_kind}), so it cannot be added to another item."
            )
        parent_ids, add_on_ids = pk_set, {instance.pk}
    else:
        parent_ids, add_on_ids = {instance.pk}, pk_set

    if parent_ids & add_on_ids:
        raise ValidationError("An item cannot be its own add-on.")

    tenant_ids = set(
        CatalogItem.objects.filter(pk__in=parent_ids | add_on_ids).values_list(
            "tenant_id", flat=True
        )
    )
    if len(tenant_ids) > 1:
        raise ValidationError("Add-ons must belong to the same tenant as the item.")

    if not reverse:
        wrong_kind = list(
            CatalogItem.objects.filter(pk__in=add_on_ids)
            .exclude(item_kind=CatalogItem.ItemKind.ADD_ON)
            .values_list("name", flat=True)
        )
        if wrong_kind:
            raise ValidationError(
                "Only items with item_kind=ADD_ON can be attached as add-ons. "
                f"Rejected: {', '.join(wrong_kind)}."
            )
