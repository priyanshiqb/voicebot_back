"""
utils.querysets — keyset paging that holds no server-side cursor.

These helpers replace `.iterator()` because pgbouncer's transaction pooling drops a
named cursor between fetches (InvalidCursorName). That failure needs pgbouncer to
reproduce, so these pin what the replacement must guarantee instead: every row exactly
once, in the requested order, across page boundaries and ties, even when the loop
changes the rows it is walking.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.test import TestCase

from core.models.contact import Contact
from tenant.models.Tenant import Tenant
from utils.querysets import batched_queryset, batched_values

UTC = ZoneInfo("UTC")


class BatchedQuerysetTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-batched")
        self.contacts = [
            Contact.objects.create(
                tenant=self.tenant,
                first_name=f"Person{i}",
                phone_number=f"+1415555{i + 1:04d}",
            )
            for i in range(7)
        ]
        self.pks = sorted(contact.pk for contact in self.contacts)

    def _queryset(self):
        return Contact.objects.filter(tenant=self.tenant)

    def test_pages_every_row_once_in_pk_order(self):
        pks = [contact.pk for contact in batched_queryset(self._queryset(), batch_size=3)]

        self.assertEqual(pks, self.pks)

    def test_custom_ordering_survives_a_tie_across_a_page_boundary(self):
        """Five rows share one created_at, so the tie spans three pages of two."""
        tied = [contact.pk for contact in self.contacts[:5]]
        Contact.objects.filter(pk__in=tied).update(
            created_at=datetime(2027, 1, 1, tzinfo=UTC)
        )
        Contact.objects.exclude(pk__in=tied).filter(tenant=self.tenant).update(
            created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        expected = list(
            self._queryset().order_by("created_at", "id").values_list("pk", flat=True)
        )

        pks = [
            contact.pk
            for contact in batched_queryset(
                self._queryset(), batch_size=2, order_by=("created_at", "id")
            )
        ]

        self.assertEqual(pks, expected)

    def test_rows_updated_out_of_the_filter_do_not_shift_later_pages(self):
        """What offset paging gets wrong: the loop changes what the queryset matches."""
        seen = []
        for contact in batched_queryset(self._queryset().filter(is_active=True), batch_size=2):
            seen.append(contact.pk)
            Contact.objects.filter(pk=contact.pk).update(is_active=False)

        self.assertEqual(sorted(seen), self.pks)

    def test_descending_ordering_is_refused(self):
        """A keyset on a descending field would page the wrong way and skip rows."""
        with self.assertRaises(ValueError):
            list(batched_queryset(self._queryset(), order_by=("-created_at", "id")))

    def test_a_short_last_page_ends_without_an_extra_query(self):
        with self.assertNumQueries(3):  # pages of 3, 3 and 1
            self.assertEqual(len(list(batched_queryset(self._queryset(), batch_size=3))), 7)

    def test_a_full_last_page_needs_one_empty_query_to_know_it_is_done(self):
        with self.assertNumQueries(2):  # 7 rows, then an empty page
            self.assertEqual(len(list(batched_queryset(self._queryset(), batch_size=7))), 7)

    def test_batched_values_yields_the_fields_then_the_pk(self):
        rows = list(batched_values(self._queryset(), ["first_name"], batch_size=3))

        self.assertEqual([row[-1] for row in rows], self.pks)
        self.assertEqual(
            {row[0] for row in rows}, {f"Person{i}" for i in range(7)}
        )
