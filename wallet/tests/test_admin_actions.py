"""Section 27 of the billing test plan — AD-01 to AD-22 and AD-26.

The admin glue was untested even though the services beneath it are — and these
actions are how support moves real money.

The load-bearing property, tested throughout §27.1: **the admin is never a second
writer.** Grant and Deduct go through ``wallet.services.wallet.adjust()``, which takes
the row lock, writes the ledger entry and keeps the buckets consistent in one
transaction. A balance edited in a form would change while the history that explains it
did not, and nothing would ever report the discrepancy.

Actions are invoked directly rather than through the admin HTTP layer: the site
requires OTP, and what is under test is the action, not Django's changelist.
"""

from datetime import timedelta

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.utils import timezone

from onboard.tests.billing_factories import (
    assert_invariant,
    make_plan,
    make_tenant,
)
from wallet.admin.credit_ledger import CreditLedgerAdmin
from wallet.admin.credit_rate import CreditRateAdmin, CreditRateInline
from wallet.admin.wallet import CreditLedgerInline, WalletAdmin
from wallet.models import CreditLedger, CreditRate, Wallet
from wallet.services import wallet as wallet_service

User = get_user_model()

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Operation = CreditRate.Operation


class AdminActionTestCase(TestCase):
    """A staff user, a bound admin, and a request that can carry messages."""

    def setUp(self):
        self.site = AdminSite()
        self.factory = RequestFactory()
        self.staff = User.objects.create_superuser(
            email="support@example.com", username="support", password="x"
        )

    def request(self, **post):
        """A POST request with a working message store.

        ``message_user`` writes to the messages framework, which needs a session and
        a storage backend that ``RequestFactory`` does not provide.
        """
        request = self.factory.post("/admin/", post)
        request.user = self.staff
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def messages(self, request):
        return [str(message) for message in request._messages]


class WalletAdminActionTests(AdminActionTestCase):
    """AD-01 to AD-16."""

    def setUp(self):
        super().setUp()
        self.admin = WalletAdmin(Wallet, self.site)
        self.ctx = make_tenant("admin-wallet")
        self.tenant = self.ctx.tenant
        self.plan = make_plan("admin-wallet-plan")
        wallet_service.set_plan_context(self.tenant, self.plan)
        wallet_service.grant(
            self.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
            amount=1000, reference_id="admin-seed",
        )

    @property
    def wallet(self):
        return Wallet.objects.get(tenant=self.tenant)

    def wallets(self):
        return Wallet.objects.filter(tenant=self.tenant)

    def grant(self, amount="500", reason="TICKET-1 goodwill"):
        request = self.request(
            adjustment_amount=amount, adjustment_reason=reason
        )
        self.admin.grant_credits(request, self.wallets())
        return request

    def deduct(self, amount="500", reason="TICKET-2 correction"):
        request = self.request(
            adjustment_amount=amount, adjustment_reason=reason
        )
        self.admin.deduct_credits(request, self.wallets())
        return request

    # --- granting -----------------------------------------------------------

    def test_ad01_a_grant_moves_credits_through_the_service(self):
        """AD-01: **through the service, never a field.**

        The proof is the ledger row: a form that wrote the balance directly would
        leave the balance changed and the history silent.
        """
        self.grant(amount="500")

        wallet = assert_invariant(self, self.tenant, msg="after an admin grant")
        self.assertEqual(wallet.purchased_balance, 500)
        self.assertTrue(
            CreditLedger.all_objects.filter(
                tenant=self.tenant, entry_type=EntryType.ADMIN_ADJUSTMENT, amount=500
            ).exists()
        )

    def test_ad02_the_acting_staff_user_is_recorded(self):
        """AD-02: an unattributed movement is indistinguishable from a bug."""
        self.grant()

        entry = CreditLedger.all_objects.get(
            tenant=self.tenant, entry_type=EntryType.ADMIN_ADJUSTMENT
        )
        self.assertEqual(entry.performed_by_id, self.staff.pk)
        self.assertIn("TICKET-1", entry.notes)

    def test_ad03_a_grant_with_no_amount_moves_nothing(self):
        """AD-03."""
        request = self.grant(amount="")

        self.assertEqual(self.wallet.balance, 1000)
        self.assertIn("Enter a number of credits", " ".join(self.messages(request)))

    def test_ad04_a_grant_with_no_reason_is_refused(self):
        """AD-04: **an unexplained balance change is worse than none.**

        Six months later the only question anyone asks about this row is "why", and
        a blank field cannot answer it.
        """
        request = self.grant(reason="")

        self.assertEqual(self.wallet.balance, 1000)
        self.assertIn("reason", " ".join(self.messages(request)).lower())

    def test_ad05_a_non_numeric_amount_moves_nothing(self):
        """AD-05: and says what it saw, so the operator can spot the typo."""
        request = self.grant(amount="five hundred")

        self.assertEqual(self.wallet.balance, 1000)
        self.assertIn("five hundred", " ".join(self.messages(request)))

    def test_ad06_an_amount_below_one_is_refused(self):
        """AD-06: a zero-credit movement is either a mistake or noise, and the
        ledger's non-zero constraint would reject it anyway."""
        for amount in ("0", "-50"):
            request = self.grant(amount=amount)
            self.assertEqual(self.wallet.balance, 1000)
            self.assertIn("positive", " ".join(self.messages(request)).lower())

    # --- deducting ----------------------------------------------------------

    def test_ad07_a_deduction_drains_allocated_first(self):
        """AD-07: same ordering as a real charge, so a correction cannot quietly
        reshape which bucket the tenant's credits sit in."""
        wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP, amount=200,
            bucket=Bucket.PURCHASED, reference_id="admin-purchased",
        )

        self.deduct(amount="300")

        wallet = assert_invariant(self, self.tenant, msg="after an admin deduction")
        self.assertEqual(wallet.allocated_balance, 700)
        self.assertEqual(wallet.purchased_balance, 200)

    def test_ad08_deducting_more_than_is_held_never_goes_negative(self):
        """AD-08."""
        self.deduct(amount="999999")

        wallet = assert_invariant(self, self.tenant, msg="after over-deducting")
        self.assertEqual(wallet.balance, 0)

    def test_ad09_an_action_across_several_wallets_reports_what_it_did(self):
        """AD-09: support selects a page of wallets; a silent action leaves them
        guessing which ones took."""
        second = make_tenant("admin-wallet-second")
        wallet_service.set_plan_context(second.tenant, self.plan)
        wallet_service.grant(
            second.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
            amount=100, reference_id="admin-seed-2",
        )
        queryset = Wallet.objects.filter(
            tenant__in=[self.tenant, second.tenant]
        )
        request = self.request(
            adjustment_amount="250", adjustment_reason="TICKET-9 bulk"
        )

        self.admin.grant_credits(request, queryset)

        self.assertEqual(self.wallet.purchased_balance, 250)
        self.assertEqual(
            Wallet.objects.get(tenant=second.tenant).purchased_balance, 250
        )
        self.assertIn("on 2 wallet(s)", " ".join(self.messages(request)))
        assert_invariant(self, self.tenant)
        assert_invariant(self, second.tenant)

    def test_ad10_a_double_submitted_form_pays_out_once(self):
        """AD-10: **a double-clicked action must not double the goodwill.**

        The reference carries a second-precision timestamp, so two submissions
        inside the same second collide on the ledger's idempotency constraint.
        """
        stamp = timezone.now().strftime("%Y%m%d%H%M%S")
        wallet = self.wallet
        reference = f"admin:{wallet.pk}:{stamp}"

        first = wallet_service.adjust(
            self.tenant, amount=500, reference_id=reference,
            performed_by=self.staff, notes="double click",
        )
        second = wallet_service.adjust(
            self.tenant, amount=500, reference_id=reference,
            performed_by=self.staff, notes="double click",
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(self.wallet.purchased_balance, 500)
        assert_invariant(self, self.tenant)

    def test_ad11_a_deliberate_repeat_later_applies_again(self):
        """AD-11: the guard is against a double submission, not against support
        granting the same amount twice on purpose."""
        wallet = self.wallet
        earlier = (timezone.now() - timedelta(minutes=1)).strftime("%Y%m%d%H%M%S")
        later = timezone.now().strftime("%Y%m%d%H%M%S")

        wallet_service.adjust(
            self.tenant, amount=500, reference_id=f"admin:{wallet.pk}:{earlier}",
            performed_by=self.staff, notes="first grant",
        )
        wallet_service.adjust(
            self.tenant, amount=500, reference_id=f"admin:{wallet.pk}:{later}",
            performed_by=self.staff, notes="second grant",
        )

        self.assertEqual(self.wallet.purchased_balance, 1000)
        assert_invariant(self, self.tenant)

    # --- state --------------------------------------------------------------

    def test_ad12_freezing_names_the_staff_member_who_did_it(self):
        """AD-12: "frozen by admin" is unactionable; "frozen by support@..." is not."""
        request = self.request()

        self.admin.freeze_wallets(request, self.wallets())

        wallet = self.wallet
        self.assertTrue(wallet.is_frozen)
        self.assertIn(str(self.staff), wallet.frozen_reason)
        self.assertIsNotNone(wallet.frozen_at)

    def test_ad13_unfreezing_clears_the_reason_and_the_timestamp(self):
        """AD-13: a stale reason on a live wallet reads as an ongoing problem."""
        self.admin.freeze_wallets(self.request(), self.wallets())

        self.admin.unfreeze_wallets(self.request(), self.wallets())

        wallet = self.wallet
        self.assertFalse(wallet.is_frozen)
        self.assertIsNone(wallet.frozen_reason)
        self.assertIsNone(wallet.frozen_at)

    def test_ad14_no_balance_field_is_editable(self):
        """AD-14: **the field that must never be typed into.**

        A balance is a cache of the ledger. Editing one here would break the
        invariant silently — the number changes while the history that explains it
        does not.
        """
        readonly = set(self.admin.readonly_fields)

        for field in (
            "balance", "allocated_balance", "purchased_balance",
            "lifetime_granted", "lifetime_consumed",
            "is_frozen", "frozen_reason", "frozen_at",
            "current_plan", "rate_snapshot", "rate_snapshot_at",
        ):
            self.assertIn(field, readonly, f"{field} is editable in the wallet admin")

    def test_ad15_one_failing_wallet_does_not_stop_the_others(self):
        """AD-15: surfaced to the operator, and the rest of the selection still
        processed — a page-wide action that aborts halfway is worse than one that
        reports what it could not do."""
        from unittest.mock import patch

        second = make_tenant("admin-wallet-failing")
        wallet_service.set_plan_context(second.tenant, self.plan)
        queryset = Wallet.objects.filter(
            tenant__in=[self.tenant, second.tenant]
        ).order_by("created_at")

        real_adjust = wallet_service.adjust
        seen = []

        def explode_once(tenant, **kwargs):
            seen.append(tenant)
            if len(seen) == 1:
                raise RuntimeError("wallet locked by another process")
            return real_adjust(tenant, **kwargs)

        with patch.object(wallet_service, "adjust", side_effect=explode_once):
            request = self.request(
                adjustment_amount="100", adjustment_reason="TICKET-15"
            )
            self.admin.grant_credits(request, queryset)

        messages = " ".join(self.messages(request))
        self.assertIn("wallet locked by another process", messages)
        self.assertIn("on 1 wallet(s)", messages)

    def test_ad16_the_ledger_inline_offers_no_way_to_add_a_row(self):
        """AD-16: the inline exists to answer "why is it that number", and an
        editable history could not."""
        inline = CreditLedgerInline(Wallet, self.site)

        self.assertFalse(inline.has_add_permission(self.request()))
        self.assertFalse(inline.can_delete)
        self.assertEqual(set(inline.fields), set(inline.readonly_fields))


class CreditRateAdminActionTests(AdminActionTestCase):
    """AD-17 to AD-22."""

    def setUp(self):
        super().setUp()
        self.admin = CreditRateAdmin(CreditRate, self.site)
        self.plan = make_plan("admin-rate-plan", with_rates=False)

    def rate(self, operation=Operation.CONVERSATION_HIGH_LATENCY, **overrides):
        fields = {
            "plan": self.plan,
            "operation": operation,
            "unit": CreditRate.Unit.PER_MINUTE,
            "credits_per_unit": 5,
            "effective_from": timezone.now() - timedelta(days=30),
        }
        fields.update(overrides)
        return CreditRate.objects.create(**fields)

    def supersede(self, queryset):
        request = self.request()
        self.admin.supersede_rates(request, queryset)
        return request

    def test_ad17_superseding_closes_the_selected_rate(self):
        """AD-17: **rates are superseded, never edited.**

        Changing ``credits_per_unit`` in place rewrites what a plan advertised
        historically, and "when did Pro go to 3 credits a minute" stops having an
        answer.
        """
        rate = self.rate()

        self.supersede(CreditRate.objects.filter(pk=rate.pk))

        rate.refresh_from_db()
        self.assertIsNotNone(rate.effective_to)
        self.assertFalse(rate.is_current)

    def test_ad18_superseding_a_rate_created_moments_ago_does_not_raise(self):
        """AD-18: **the fast-path bug.**

        ``ck_credit_rate_period_valid`` rejects a zero-length window — correctly,
        since such a row was never in force. Setting ``effective_to`` blindly to now
        would raise an IntegrityError on exactly the rate someone just noticed was
        wrong, so it is clamped past ``effective_from`` instead.
        """
        rate = self.rate(effective_from=timezone.now() + timedelta(minutes=5))

        self.supersede(CreditRate.objects.filter(pk=rate.pk))

        rate.refresh_from_db()
        self.assertIsNotNone(rate.effective_to)
        self.assertGreater(rate.effective_to, rate.effective_from)

    def test_ad19_an_already_closed_rate_is_skipped_and_counted(self):
        """AD-19: re-closing would move a historical boundary, which is the one
        thing versioning exists to prevent."""
        closed_at = timezone.now() - timedelta(days=1)
        rate = self.rate(effective_to=closed_at)

        request = self.supersede(CreditRate.objects.filter(pk=rate.pk))

        rate.refresh_from_db()
        self.assertEqual(rate.effective_to, closed_at)
        self.assertIn("Skipped 1", " ".join(self.messages(request)))

    def test_ad20_superseding_frees_the_slot_for_a_replacement(self):
        """AD-20: the whole point — the uniqueness constraint allows exactly one
        open-ended rate per plan and operation."""
        rate = self.rate()
        self.supersede(CreditRate.objects.filter(pk=rate.pk))

        replacement = self.rate(credits_per_unit=3, effective_from=timezone.now())

        self.assertTrue(replacement.is_current)
        self.assertEqual(
            CreditRate.objects.filter(
                plan=self.plan,
                operation=Operation.CONVERSATION_HIGH_LATENCY,
                effective_to__isnull=True,
            ).count(),
            1,
        )

    def test_ad21_the_message_warns_that_the_plan_now_has_no_price(self):
        """AD-21: **the gap is the dangerous part.**

        Between closing a rate and adding its replacement, every call on that tier
        raises ``RateNotConfigured`` for every tenant on the plan.
        """
        rate = self.rate()

        request = self.supersede(CreditRate.objects.filter(pk=rate.pk))

        message = " ".join(self.messages(request))
        self.assertIn("no price", message)
        self.assertIn("replacement", message)

    def test_ad22_rates_are_editable_on_the_plan_they_belong_to(self):
        """AD-22: "3 credits a minute" is a sentence about a plan; read on its own
        it is trivia."""
        from onboard.admin.plan import PlanAdmin

        self.assertIn(CreditRateInline, PlanAdmin.inlines)
        inline = CreditRateInline(self.plan.__class__, self.site)
        self.assertIn("credits_per_unit", inline.fields)
        self.assertIn("effective_to", inline.fields)


class LedgerAdminTests(AdminActionTestCase):
    """AD-26."""

    def test_ad26_the_ledger_admin_offers_no_write_path_at_all(self):
        """AD-26: **the third door.**

        The model refuses updates and the queryset refuses bulk rewrites; this
        closes the last one. Every action here would raise from the model anyway, so
        leaving them on the page only offers staff a button that errors.
        """
        admin = CreditLedgerAdmin(CreditLedger, self.site)
        request = self.request()

        self.assertFalse(admin.has_add_permission(request))
        self.assertFalse(admin.has_change_permission(request))
        self.assertFalse(admin.has_delete_permission(request))
        self.assertIsNone(admin.actions)
        self.assertEqual(
            set(admin.get_readonly_fields(request)),
            {field.name for field in CreditLedger._meta.fields},
        )
