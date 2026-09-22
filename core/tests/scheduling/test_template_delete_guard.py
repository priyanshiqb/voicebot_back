"""
Deleting an event template that still has active bookings.

The guard used to live in `EventTemplateViewSet.perform_destroy`, so it covered
`DELETE /{id}/` and nothing else. `bulk_delete` calls `soft_delete()` on each row
directly and never reaches that method — which meant a template with confirmed
bookings could be removed in bulk, leaving customers holding appointments for a
meeting type that no longer existed.

Moving the check onto the model closes every path at once: REST delete, bulk
delete, and a shell session. It runs inside `soft_delete`'s existing row lock, so
a booking created concurrently cannot slip past it.

`PROTECT` on the foreign key cannot substitute for this: templates are
soft-deleted, so the database constraint never fires.
"""

from datetime import timedelta
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from core.models.booking import Booking
from core.models.event_template import EventTemplate
from core.models.user_event_skill import UserEventSkill
from core.services import bookings as booking_service
from core.tests.scheduling import factories as f
from core.tests.scheduling.factories import ATTENDEES

REQUIRES_PG = skipUnless(
    connection.vendor == "postgresql", "Scheduling tests require PostgreSQL."
)

TEMPLATES_URL = "/api/v1/core/event-templates/"


class _TemplateBase:
    def _setup_template(self):
        self.tenant = f.make_tenant()
        self.actor = f.make_user(self.tenant)
        self.template = f.make_template(self.tenant)
        self.host = f.make_host(self.tenant, self.template)
        self.now = timezone.now()

    def _book(self):
        start = (self.now + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        return booking_service.create_booking(
            tenant=self.tenant,
            event_template=self.template,
            start_time=start,
            participants=ATTENDEES,
            target_host_id=self.host.id,
            now=self.now,
        )


@REQUIRES_PG
class ModelLevelGuardTests(_TemplateBase, TestCase):
    """The guard belongs to the model, so it holds regardless of caller."""

    def setUp(self):
        self._setup_template()

    def test_soft_delete_without_bookings_succeeds(self):
        self.template.soft_delete()
        self.template.refresh_from_db()
        self.assertTrue(self.template.is_deleted)

    def test_soft_delete_refuses_while_a_booking_is_active(self):
        self._book()
        with self.assertRaises(ValidationError) as ctx:
            self.template.soft_delete()

        self.assertIn("active booking", str(ctx.exception))
        self.template.refresh_from_db()
        self.assertFalse(self.template.is_deleted)

    def test_the_refusal_counts_the_blocking_bookings(self):
        """A bare 'cannot delete' leaves an admin with nothing to act on."""
        self._book()
        with self.assertRaises(ValidationError) as ctx:
            self.template.soft_delete()
        self.assertIn("1 active booking", str(ctx.exception))

    def test_cancelled_bookings_do_not_block(self):
        booking = self._book()
        booking_service.cancel_booking(
            booking=booking, enforce_lead=False, now=self.now
        )

        self.template.soft_delete()
        self.template.refresh_from_db()
        self.assertTrue(self.template.is_deleted)

    def test_soft_deleted_bookings_do_not_block(self):
        booking = self._book()
        Booking.objects.filter(pk=booking.pk).update(is_deleted=True)

        self.template.soft_delete()
        self.template.refresh_from_db()
        self.assertTrue(self.template.is_deleted)

    def test_name_is_still_freed_for_reuse(self):
        """The guard must not break soft_delete's other job."""
        original = self.template.name
        self.template.soft_delete()

        self.template.refresh_from_db()
        self.assertNotEqual(self.template.name, original)
        self.assertIn("-deleted-", self.template.name)
        # The freed name can be taken again.
        EventTemplate.objects.create(
            tenant=self.tenant, name=original, duration_minutes=30
        )


@REQUIRES_PG
class DeletionPathTests(_TemplateBase, APITestCase):
    """Both HTTP routes to deletion must refuse identically."""

    def setUp(self):
        self._setup_template()
        self.client = APIClient()
        self.client.force_authenticate(user=self.actor)

    def test_single_delete_refuses(self):
        self._book()
        response = self.client.delete(f"{TEMPLATES_URL}{self.template.id}/")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("active booking", str(response.data))
        self.template.refresh_from_db()
        self.assertFalse(self.template.is_deleted)

    def test_bulk_delete_refuses_too(self):
        """The path the guard used to miss entirely."""
        self._book()
        response = self.client.post(
            f"{TEMPLATES_URL}bulk_delete/",
            {"ids": [str(self.template.id)]},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("active booking", str(response.data))
        self.template.refresh_from_db()
        self.assertFalse(self.template.is_deleted)

    def test_bulk_delete_is_all_or_nothing_when_one_is_blocked(self):
        """A blocked template must not leave its siblings half-deleted."""
        clean = f.make_template(self.tenant)
        self._book()  # blocks self.template only

        response = self.client.post(
            f"{TEMPLATES_URL}bulk_delete/",
            {"ids": [str(clean.id), str(self.template.id)]},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        clean.refresh_from_db()
        self.template.refresh_from_db()
        self.assertFalse(self.template.is_deleted)
        self.assertFalse(clean.is_deleted, "a sibling was deleted despite the failure")

    def test_bulk_delete_succeeds_when_nothing_is_booked(self):
        other = f.make_template(self.tenant)
        response = self.client.post(
            f"{TEMPLATES_URL}bulk_delete/",
            {"ids": [str(self.template.id), str(other.id)]},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.template.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(self.template.is_deleted)
        self.assertTrue(other.is_deleted)

    def test_deactivating_is_the_escape_hatch(self):
        """Documented alternative: stop new bookings, honour existing ones.

        Deactivating needs the type released first — no active bot or bookable host may
        still offer it — so the host's event skill is switched off before the type is.
        """
        self._book()
        UserEventSkill.objects.filter(
            user=self.host, event_template=self.template
        ).update(is_active=False)
        response = self.client.patch(
            f"{TEMPLATES_URL}{self.template.id}/", {"is_active": False}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.template.refresh_from_db()
        self.assertFalse(self.template.is_active)
        self.assertFalse(self.template.is_deleted)
