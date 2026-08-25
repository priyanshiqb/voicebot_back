"""Section 2 of the billing test plan — LI-01 to LI-15.

``Wallet.balance`` is a cache of ``SUM(CreditLedger.amount)``. That statement is worth
nothing unless the rows are genuinely append-only, so immutability is closed at three
doors — the model, the queryset, and the admin — and each door is tested here.

The one that matters most is LI-07. ``BaseQuerySet.soft_delete()`` issues a plain
``UPDATE is_deleted = true``, and the default manager filters those rows out: a
soft-deleted ledger row would silently vanish from every sum while still sitting in the
table, and no reconciliation job would ever flag it.
"""

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase

from onboard.tests.billing_factories import make_tenant
from wallet.admin.credit_ledger import CreditLedgerAdmin
from wallet.models import CreditLedger, Wallet

User = get_user_model()

EntryType = CreditLedger.EntryType


class LedgerImmutabilityTests(TestCase):
    """LI-01 to LI-10 — every write path that is not an insert."""

    def setUp(self):
        self.tenant = make_tenant("ledger-immutable").tenant
        self.wallet = Wallet.objects.create(
            tenant=self.tenant, balance=100, allocated_balance=100
        )
        self.entry = self._entry()

    def _entry(self, **overrides):
        fields = {
            "tenant": self.tenant,
            "wallet": self.wallet,
            "entry_type": EntryType.CYCLE_ALLOCATION,
            "bucket": CreditLedger.Bucket.ALLOCATED,
            "amount": 100,
            "balance_after": 100,
        }
        fields.update(overrides)
        return CreditLedger.objects.create(**fields)

    # --- instance-level writes ---------------------------------------------

    def test_li01_saving_a_persisted_row_raises(self):
        """LI-01: the row exists, so this is an UPDATE, so it is refused."""
        self.entry.amount = 999

        with self.assertRaises(NotImplementedError):
            self.entry.save()

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.amount, 100)

    def test_li02_deleting_an_instance_raises(self):
        """LI-02."""
        with self.assertRaises(NotImplementedError):
            self.entry.delete()

        self.assertTrue(CreditLedger.all_objects.filter(pk=self.entry.pk).exists())

    def test_li03_soft_deleting_an_instance_raises(self):
        """LI-03: the dangerous one at instance level — the row would stay in the
        table but drop out of every sum."""
        with self.assertRaises(NotImplementedError):
            self.entry.soft_delete()

        self.entry.refresh_from_db()
        self.assertFalse(self.entry.is_deleted)

    def test_li04_restore_raises_because_it_routes_through_save(self):
        """LI-04: inherited from BaseModelCore, and blocked by the save override
        rather than by a second explicit refusal."""
        with self.assertRaises(NotImplementedError):
            self.entry.restore()

    def test_li05_deactivate_raises_because_it_routes_through_save(self):
        """LI-05."""
        with self.assertRaises(NotImplementedError):
            self.entry.deactivate()

        self.entry.refresh_from_db()
        self.assertTrue(self.entry.is_active)

    # --- queryset-level writes ---------------------------------------------

    def test_li06_queryset_delete_raises(self):
        """LI-06."""
        with self.assertRaises(NotImplementedError):
            CreditLedger.objects.filter(pk=self.entry.pk).delete()

        self.assertTrue(CreditLedger.all_objects.filter(pk=self.entry.pk).exists())

    def test_li07_queryset_soft_delete_raises(self):
        """LI-07: **the one that would silently break every sum.**

        Without this override the row stays in the table, disappears from the default
        manager, and ``SUM(amount)`` quietly stops matching ``Wallet.balance`` with
        nothing raised anywhere.
        """
        with self.assertRaises(NotImplementedError):
            CreditLedger.objects.filter(pk=self.entry.pk).soft_delete()

        self.entry.refresh_from_db()
        self.assertFalse(self.entry.is_deleted)
        self.assertTrue(CreditLedger.objects.filter(pk=self.entry.pk).exists())

    def test_li08_queryset_update_raises(self):
        """LI-08: bulk update bypasses ``save()`` entirely, so it needs its own door."""
        with self.assertRaises(NotImplementedError):
            CreditLedger.objects.filter(pk=self.entry.pk).update(amount=1)

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.amount, 100)

    def test_li09_all_objects_is_blocked_identically(self):
        """LI-09: ``all_objects`` exists for idempotency probes, not as a back door."""
        queryset = CreditLedger.all_objects.filter(pk=self.entry.pk)

        with self.assertRaises(NotImplementedError):
            queryset.delete()
        with self.assertRaises(NotImplementedError):
            queryset.soft_delete()
        with self.assertRaises(NotImplementedError):
            queryset.update(amount=1)

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.amount, 100)
        self.assertFalse(self.entry.is_deleted)

    def test_li10_inserting_a_new_row_is_allowed(self):
        """LI-10: append-only means append *is* allowed. Without this the other nine
        cases would pass on a model that simply cannot be written to."""
        created = self._entry(
            entry_type=EntryType.MANUAL_TOP_UP,
            bucket=CreditLedger.Bucket.PURCHASED,
            amount=50,
            balance_after=150,
        )

        self.assertIsNotNone(created.pk)
        self.assertEqual(CreditLedger.objects.filter(wallet=self.wallet).count(), 2)

    def test_li11_admin_offers_no_write_path(self):
        """LI-11: the third door. Every one of these would raise from the model, so
        leaving them on the page only offers staff a button that errors."""
        admin = CreditLedgerAdmin(CreditLedger, None)
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser(
            email="ledger-admin@example.com", username="ledger-admin", password="x"
        )

        self.assertFalse(admin.has_add_permission(request))
        self.assertFalse(admin.has_change_permission(request))
        self.assertFalse(admin.has_change_permission(request, self.entry))
        self.assertFalse(admin.has_delete_permission(request))
        self.assertFalse(admin.has_delete_permission(request, self.entry))
        self.assertIsNone(admin.actions)

        # Read-only covers every concrete field, not a hand-maintained subset that
        # goes stale the next time a column is added.
        readonly = set(admin.get_readonly_fields(request, self.entry))
        self.assertEqual(
            readonly, {f.name for f in CreditLedger._meta.fields}
        )


class LedgerConstraintTests(TestCase):
    """LI-12 to LI-15 — what the database itself refuses."""

    def setUp(self):
        self.tenant = make_tenant("ledger-constraints").tenant
        self.wallet = Wallet.objects.create(
            tenant=self.tenant, balance=100, allocated_balance=100
        )

    def _entry(self, **overrides):
        fields = {
            "tenant": self.tenant,
            "wallet": self.wallet,
            "entry_type": EntryType.CYCLE_ALLOCATION,
            "amount": 100,
            "balance_after": 100,
        }
        fields.update(overrides)
        return CreditLedger.objects.create(**fields)

    def test_li12_a_zero_amount_row_is_refused(self):
        """LI-12: ck_ledger_amount_non_zero. A zero movement is either a bug or
        noise in the audit trail, and both are worth failing over."""
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                self._entry(amount=0)

        self.assertIn("ck_ledger_amount_non_zero", str(caught.exception))

    def test_li13_a_reference_cannot_be_recorded_twice_for_one_entry_type(self):
        """LI-13: uniq_ledger_entry_reference — the idempotency guarantee.

        This is what turns a redelivered webhook or a retried runner POST into a
        no-op instead of a double charge.
        """
        self._entry(reference_id="pay_abc123")

        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                self._entry(reference_id="pay_abc123")

        self.assertIn("uniq_ledger_entry_reference", str(caught.exception))

    def test_li14_one_reference_can_appear_under_different_entry_types(self):
        """LI-14: one real-world event legitimately produces several rows.

        A renewal payment grants credits and forfeits the unrolled remainder, and both
        rows point at the same order id. Keying on the reference alone would refuse
        the second.
        """
        self._entry(entry_type=EntryType.CYCLE_ALLOCATION, reference_id="order_xyz")
        self._entry(
            entry_type=EntryType.ROLLOVER_FORFEIT,
            reference_id="order_xyz",
            amount=-20,
            balance_after=80,
        )

        self.assertEqual(
            CreditLedger.objects.filter(reference_id="order_xyz").count(), 2
        )

    def test_li15_many_rows_may_have_no_reference_at_all(self):
        """LI-15: the constraint is partial on ``reference_id IS NOT NULL``.

        Plenty of rows have nothing to point at — an untraceable manual adjustment,
        say — and they must not collide with each other.
        """
        for amount in (10, -10, 25):
            self._entry(
                entry_type=EntryType.ADMIN_ADJUSTMENT,
                amount=amount,
                reference_id=None,
            )

        self.assertEqual(
            CreditLedger.objects.filter(
                wallet=self.wallet, reference_id__isnull=True
            ).count(),
            3,
        )
