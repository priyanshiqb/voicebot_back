"""Section 31 of the billing test plan — MG-01 to MG-20.

**What is otherwise untested.** The suite builds its database by running every
migration against an *empty* one, so the ``RunPython`` steps in `0025` execute and
process zero rows. They are proven syntactically valid and nothing more.

Those steps run **exactly once**, against a real database with real rows, and then
never again. A bug in one silently corrupts data at the moment it runs, and there is no
second chance to notice. `0025` backfilled 35 subscriptions and 17 transactions on the
dev database; `0026` then made a column ``NOT NULL`` on the strength of that having
worked.

**MG-07 first.** Its duplicate-resolution branch edits live billing state, and on the
dev database it happened to find nothing — so it has never actually run anywhere. It is
the only code in this cycle that has shipped completely unexercised.

Three details decide whether this technique works at all:

* ``TransactionTestCase``, not ``TestCase``. Migrations run DDL, which cannot happen
  inside a test transaction that will be rolled back.
* **Historical models only**, from the executor's project state. Importing
  ``onboard.models.Subscription`` gives *today's* model — with the ``save()`` override
  that fills ``end_date``, which is precisely the behaviour the migration is supposed
  to be supplying. The test would pass while the migration did nothing.
* **Rebuild the graph after rewinding**, or the forward migrate uses a stale plan.
"""

from datetime import timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

ONBOARD_BEFORE = ("onboard", "0024_plan_renews_without_payment_and_more")
ONBOARD_0025 = ("onboard", "0025_subscription_canceled_at_and_more")
ONBOARD_0026 = ("onboard", "0026_subscription_end_date_required")
WALLET_BEFORE = ("wallet", "0003_alter_creditrate_operation_alter_creditrate_unit")
WALLET_0004 = ("wallet", "0004_backfill_tenant_wallets")
WALLET_0005 = ("wallet", "0005_seed_billing_catalogue")
TENANT_0010 = ("tenant", "0010_tenantsettings_entitlements_enforced")


class MigrationTestCase(TransactionTestCase):
    """Rewind to a point in history, build a world, and migrate forward.

    ``tearDown`` always returns the database to the latest state — a test that left
    it rewound would break every test that ran after it, in ways that look nothing
    like a migration problem.
    """

    #: Set by subclasses.
    migrate_from = None
    migrate_to = None

    def rewind(self, target=None):
        executor = MigrationExecutor(connection)
        executor.migrate([target or self.migrate_from])
        executor.loader.build_graph()
        return executor.loader.project_state([target or self.migrate_from]).apps

    def forward(self, target=None):
        target = target or self.migrate_to
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    # --- world building, with historical models ------------------------------

    def make_tenant(self, apps, name="mig", deleted=False):
        Tenant = apps.get_model("tenant", "Tenant")
        return Tenant.objects.create(
            name=name, subdomain=f"{name}-{timezone.now().timestamp()}",
            is_deleted=deleted,
        )

    def make_plan(self, apps, code="mig-plan", **overrides):
        Plan = apps.get_model("onboard", "Plan")
        fields = {
            "code": code,
            "name": f"Migration Plan {code}",
            "description": "built by a migration test",
            "base_price": Decimal("999.00"),
            "discount_percentage": Decimal("0.00"),
            "tax_percentage": Decimal("18.00"),
            "currency": "INR",
            "billing_cycle": "monthly",
            "plan_type": "paid",
            "allocated_credits": 1000,
        }
        fields.update(overrides)
        return Plan.objects.create(**fields)


class Migration0025Tests(MigrationTestCase):
    """MG-01 to MG-12."""

    migrate_from = ONBOARD_BEFORE
    migrate_to = ONBOARD_0025

    def subscription(self, apps, tenant, plan, **overrides):
        Subscription = apps.get_model("onboard", "Subscription")
        fields = {
            "tenant": tenant,
            "plan": plan,
            "status": "active",
            "start_date": timezone.now() - timedelta(days=5),
            "end_date": None,
        }
        fields.update(overrides)
        return Subscription.objects.create(**fields)

    def reloaded(self, apps, pk):
        return apps.get_model("onboard", "Subscription").objects.get(pk=pk)

    # --- end_date backfill --------------------------------------------------

    def test_mg01_a_monthly_subscription_gets_a_calendar_month(self):
        """MG-01: **end_date was never written by any of the three signup paths**,
        so expiry, renewal windows and proration had nothing to compute against."""
        old = self.rewind()
        tenant = self.make_tenant(old, "mg01")
        plan = self.make_plan(old, "mg01-plan", billing_cycle="monthly")
        start = timezone.now() - timedelta(days=5)
        pk = self.subscription(old, tenant, plan, start_date=start).pk

        new = self.forward()

        row = self.reloaded(new, pk)
        self.assertIsNotNone(row.end_date)
        # Calendar arithmetic, not thirty days: the length depends on the month.
        self.assertEqual(row.end_date, start + relativedelta(months=1))

    def test_mg02_a_yearly_subscription_gets_a_year(self):
        """MG-02."""
        old = self.rewind()
        tenant = self.make_tenant(old, "mg02")
        plan = self.make_plan(old, "mg02-plan", billing_cycle="yearly")
        start = timezone.now() - timedelta(days=5)
        pk = self.subscription(old, tenant, plan, start_date=start).pk

        new = self.forward()

        row = self.reloaded(new, pk)
        self.assertEqual(row.end_date.year, start.year + 1)

    def test_mg03_the_start_date_fallback_is_defensive_and_unreachable(self):
        """MG-03: **the fallback chain cannot fire, and that is worth recording.**

        ``backfill_cycle_and_snapshot`` reads ``start_date or created_at or now``.
        But ``start_date`` has been ``NOT NULL`` since long before `0024`, so the
        database refuses the row that would exercise the other two branches. They
        are defensive, not dead-wrong — and this test says so rather than
        pretending to cover them.
        """
        old = self.rewind()
        tenant = self.make_tenant(old, "mg03")
        plan = self.make_plan(old, "mg03-plan")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.subscription(old, tenant, plan, start_date=None)

        # And the reachable branch does its job.
        start = timezone.now() - timedelta(days=3)
        pk = self.subscription(old, tenant, plan, start_date=start).pk

        new = self.forward()

        self.assertEqual(
            self.reloaded(new, pk).end_date, start + relativedelta(months=1)
        )

    def test_mg04_an_existing_end_date_is_left_alone(self):
        """MG-04: **a row that already has a cycle has one for a reason.**"""
        old = self.rewind()
        tenant = self.make_tenant(old, "mg04")
        plan = self.make_plan(old, "mg04-plan")
        chosen = timezone.now() + timedelta(days=99)
        pk = self.subscription(old, tenant, plan, end_date=chosen).pk

        new = self.forward()

        self.assertEqual(self.reloaded(new, pk).end_date, chosen)

    # --- snapshot backfill --------------------------------------------------

    def test_mg05_a_snapshot_without_credit_rules_is_regenerated(self):
        """MG-05: **plan_snapshot froze pricing only.**

        The credit rules and entitlement allowances added since are missing from
        every existing row, and the rollover engine reads them.
        """
        old = self.rewind()
        tenant = self.make_tenant(old, "mg05")
        plan = self.make_plan(old, "mg05-plan")
        PlanEntitlement = old.get_model("onboard", "PlanEntitlement")
        PlanEntitlement.objects.create(
            plan=plan, resource="agent", included_quantity=3,
            is_addon_available=False, price_per_additional=Decimal("0.00"),
            addon_step=1,
        )
        pk = self.subscription(
            old, tenant, plan,
            plan_snapshot={"code": plan.code, "base_price": "999.00"},
        ).pk

        new = self.forward()

        snapshot = self.reloaded(new, pk).plan_snapshot
        for key in (
            "allocated_credits", "cost_per_additional_credit",
            "max_rollover_credits", "allow_rollover_on_renewal",
            "allow_rollover_on_upgrade", "allow_rollover_on_downgrade",
            "allow_use_after_expiry", "low_balance_threshold",
            "renews_without_payment",
        ):
            self.assertIn(key, snapshot, f"{key} was not backfilled")
        self.assertEqual(snapshot["entitlements"], {"agent": 3})

    def test_mg06_a_snapshot_that_already_has_them_is_not_rewritten(self):
        """MG-06: **it is a contract.**

        Rewriting one would change the terms of a cycle the tenant already paid
        for, which is the exact thing snapshotting exists to prevent.
        """
        old = self.rewind()
        tenant = self.make_tenant(old, "mg06")
        plan = self.make_plan(old, "mg06-plan", allocated_credits=1000)
        agreed = {
            "code": plan.code,
            "base_price": "111.00",
            "allocated_credits": 5,
            "allow_rollover_on_renewal": True,
        }
        pk = self.subscription(old, tenant, plan, plan_snapshot=dict(agreed)).pk

        new = self.forward()

        snapshot = self.reloaded(new, pk).plan_snapshot
        self.assertEqual(snapshot["allocated_credits"], 5)
        self.assertEqual(snapshot["base_price"], "111.00")

    # --- duplicate resolution -----------------------------------------------

    def test_mg07_only_the_newest_active_subscription_survives(self):
        """MG-07: **the branch that has never run anywhere.**

        The uniqueness constraint cannot be applied while a tenant holds more than
        one ACTIVE row, and the newest is what the tenant last paid for. The others
        are expired, dated and linked to the survivor so support can follow the
        chain.
        """
        old = self.rewind()
        tenant = self.make_tenant(old, "mg07")
        plan = self.make_plan(old, "mg07-plan")
        oldest = self.subscription(
            old, tenant, plan, start_date=timezone.now() - timedelta(days=90)
        )
        middle = self.subscription(
            old, tenant, plan, start_date=timezone.now() - timedelta(days=60)
        )
        newest = self.subscription(
            old, tenant, plan, start_date=timezone.now() - timedelta(days=1)
        )

        new = self.forward()

        Subscription = new.get_model("onboard", "Subscription")
        survivor = Subscription.objects.get(pk=newest.pk)
        self.assertEqual(survivor.status, "active")

        for stale in (oldest, middle):
            row = Subscription.objects.get(pk=stale.pk)
            self.assertEqual(row.status, "expired")
            self.assertIsNotNone(row.canceled_at)
            self.assertEqual(row.superseded_by_id, newest.pk)

        self.assertEqual(
            Subscription.objects.filter(tenant_id=tenant.pk, status="active").count(),
            1,
        )

    def test_mg08_a_tie_on_start_date_is_broken_deterministically(self):
        """MG-08: two rows with the same start date must not resolve differently
        on two different databases."""
        old = self.rewind()
        tenant = self.make_tenant(old, "mg08")
        plan = self.make_plan(old, "mg08-plan")
        shared = timezone.now() - timedelta(days=10)
        first = self.subscription(old, tenant, plan, start_date=shared)
        second = self.subscription(old, tenant, plan, start_date=shared)

        new = self.forward()

        Subscription = new.get_model("onboard", "Subscription")
        active = Subscription.objects.filter(tenant_id=tenant.pk, status="active")
        self.assertEqual(active.count(), 1)
        # Ordering is -start_date then -created_at, so the later-created row wins.
        self.assertEqual(active.get().pk, second.pk)
        self.assertEqual(
            Subscription.objects.get(pk=first.pk).superseded_by_id, second.pk
        )

    def test_mg09_the_constraint_applies_cleanly_after_resolution(self):
        """MG-09: **the resolution and the constraint are in one migration**, and
        the order between them is what stops an ``AddConstraint`` failure on a
        database that has the defect the constraint prevents."""
        old = self.rewind()
        tenant = self.make_tenant(old, "mg09")
        plan = self.make_plan(old, "mg09-plan")
        for days in (30, 20, 10):
            self.subscription(
                old, tenant, plan,
                start_date=timezone.now() - timedelta(days=days),
            )

        new = self.forward()  # would raise if the constraint could not be added

        Subscription = new.get_model("onboard", "Subscription")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Subscription.objects.create(
                    tenant_id=tenant.pk, plan_id=plan.pk, status="active",
                    start_date=timezone.now(),
                    end_date=timezone.now() + timedelta(days=30),
                )

    # --- transaction tenant backfill ----------------------------------------

    def test_mg10_a_transaction_with_a_resolvable_user_gets_its_tenant(self):
        """MG-10: for queryability, not correctness — every existing row defaults
        to ``onboarding``, so the check constraint passes either way."""
        old = self.rewind()
        tenant = self.make_tenant(old, "mg10")
        # The real user model, not the historical one. Rewinding *onboard* pins the
        # user app's project state to an older node than the table actually is, so
        # the historical model is missing columns the live table requires. The
        # migration only follows this row as a foreign key, so nothing about the
        # user model is under test here.
        from user.models import CustomUser

        user = CustomUser.objects.create(
            email="mg10@example.com", username="mg10", password="x", is_active=True
        )
        # Assigned by id: the historical models refuse a live model instance, and
        # what the migration reads is the column, not the object.
        TenantUserMapping = old.get_model("tenant", "TenantUserMapping")
        TenantUserMapping.objects.create(user_id=user.pk, tenant_id=tenant.pk)
        Transaction = old.get_model("onboard", "Transaction")
        pk = Transaction.objects.create(
            razorpay_order_id="order_mg10", amount=Decimal("100.00"),
            status="pending", user_id=user.pk,
        ).pk

        new = self.forward()

        row = new.get_model("onboard", "Transaction").objects.get(pk=pk)
        self.assertEqual(row.tenant_id, tenant.pk)

    def test_mg11_a_transaction_with_no_user_is_left_null(self):
        """MG-11: an unattributable order must not be guessed at, and the check
        constraint has to still pass with a null tenant."""
        old = self.rewind()
        Transaction = old.get_model("onboard", "Transaction")
        pk = Transaction.objects.create(
            razorpay_order_id="order_mg11", amount=Decimal("100.00"),
            status="pending",
        ).pk

        new = self.forward()

        row = new.get_model("onboard", "Transaction").objects.get(pk=pk)
        self.assertIsNone(row.tenant_id)

    def test_mg12_clean_data_is_left_completely_alone(self):
        """MG-12: **the case that runs on every environment after the first.**

        A data step that rewrites correct rows is one that can corrupt them.
        """
        old = self.rewind()
        tenant = self.make_tenant(old, "mg12")
        plan = self.make_plan(old, "mg12-plan")
        end = timezone.now() + timedelta(days=20)
        snapshot = {
            "code": plan.code, "base_price": "999.00",
            "allocated_credits": 777, "entitlements": {"agent": 9},
        }
        pk = self.subscription(
            old, tenant, plan, end_date=end, plan_snapshot=dict(snapshot)
        ).pk

        new = self.forward()

        row = self.reloaded(new, pk)
        self.assertEqual(row.end_date, end)
        self.assertEqual(row.plan_snapshot, snapshot)
        self.assertEqual(row.status, "active")


class Migration0026Tests(MigrationTestCase):
    """MG-13 to MG-14."""

    migrate_from = ONBOARD_0025
    migrate_to = ONBOARD_0026

    def test_mg13_a_straggler_created_after_0025_is_filled_first(self):
        """MG-13: **the window between the two migrations is real.**

        An environment that ran `0025`, then created subscriptions with older code,
        would fail the ``AlterField`` with a constraint violation and no
        explanation.
        """
        old = self.rewind()
        tenant = self.make_tenant(old, "mg13")
        plan = self.make_plan(old, "mg13-plan")
        Subscription = old.get_model("onboard", "Subscription")
        pk = Subscription.objects.create(
            tenant=tenant, plan=plan, status="active",
            start_date=timezone.now(), end_date=None,
        ).pk

        new = self.forward()

        row = new.get_model("onboard", "Subscription").objects.get(pk=pk)
        self.assertIsNotNone(row.end_date)

    def test_mg14_the_column_refuses_null_afterwards(self):
        """MG-14: the point of the whole pair."""
        self.rewind()
        new = self.forward()

        Subscription = new.get_model("onboard", "Subscription")
        tenant = self.make_tenant(new, "mg14")
        plan = self.make_plan(new, "mg14-plan")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Subscription.objects.create(
                    tenant=tenant, plan=plan, status="active",
                    start_date=timezone.now(), end_date=None,
                )


class WalletMigrationTests(MigrationTestCase):
    """MG-15 to MG-17, MG-19."""

    migrate_from = WALLET_BEFORE
    migrate_to = WALLET_0004

    def test_mg15_every_live_tenant_gets_a_wallet_and_deleted_ones_do_not(self):
        """MG-15: **a wallet that springs into existence on the first call has no
        row for an admin to look at** — no threshold, nothing to stamp a rate card
        onto. Soft-deleted tenants are skipped because a wallet nobody can reach
        only makes the audit harder to read."""
        old = self.rewind()
        live = self.make_tenant(old, "mg15-live")
        deleted = self.make_tenant(old, "mg15-deleted", deleted=True)

        new = self.forward()

        Wallet = new.get_model("wallet", "Wallet")
        self.assertTrue(Wallet.objects.filter(tenant_id=live.pk).exists())
        self.assertFalse(Wallet.objects.filter(tenant_id=deleted.pk).exists())
        self.assertEqual(Wallet.objects.get(tenant_id=live.pk).balance, 0)

    def test_mg16_no_ledger_row_is_written(self):
        """MG-16: **an empty ledger sums to zero, which is exactly the balance
        being opened.**

        The plan originally called for a zero ``OPENING_BALANCE`` entry per wallet.
        That row cannot exist — ``ck_ledger_amount_non_zero`` rejects it, rightly,
        because a zero-credit movement is noise in an audit trail.
        """
        old = self.rewind()
        self.make_tenant(old, "mg16")

        new = self.forward()

        CreditLedger = new.get_model("wallet", "CreditLedger")
        self.assertEqual(CreditLedger.objects.count(), 0)

    def test_mg17_a_tenant_that_already_has_a_wallet_does_not_get_a_second(self):
        """MG-17: idempotent, so it is safe to re-run against a database where
        some already exist."""
        old = self.rewind()
        tenant = self.make_tenant(old, "mg17")
        Wallet = old.get_model("wallet", "Wallet")
        existing = Wallet.objects.create(
            tenant_id=tenant.pk, balance=250, allocated_balance=250,
            purchased_balance=0,
        )

        new = self.forward()

        NewWallet = new.get_model("wallet", "Wallet")
        wallets = NewWallet.objects.filter(tenant_id=tenant.pk)
        self.assertEqual(wallets.count(), 1)
        self.assertEqual(wallets.get().pk, existing.pk)
        self.assertEqual(wallets.get().balance, 250)

    def test_mg19_the_emptied_catalogue_migration_is_a_no_op(self):
        """MG-19: it once seeded a hard-coded plan catalogue. That was removed —
        **whatever an admin configures is final** — and the migration stays as an
        applied no-op so nobody's history diverges."""
        self.rewind()

        new = self.forward(WALLET_0005)

        CreditRate = new.get_model("wallet", "CreditRate")
        self.assertEqual(CreditRate.objects.count(), 0)


class TenantFlagMigrationTests(MigrationTestCase):
    """MG-18."""

    migrate_from = ("tenant", "0008_tenantsettings")
    migrate_to = TENANT_0010

    def test_mg18_enforcement_flags_arrive_switched_off(self):
        """MG-18: **nobody is silently enforced.**

        A default of True would have started refusing work for every existing
        tenant the moment the migration landed, including ones already over a limit
        they had never been told about.
        """
        old = self.rewind()
        tenant = self.make_tenant(old, "mg18")
        TenantSettings = old.get_model("tenant", "TenantSettings")
        pk = TenantSettings.objects.create(tenant_id=tenant.pk).pk

        new = self.forward()

        row = new.get_model("tenant", "TenantSettings").objects.get(pk=pk)
        self.assertFalse(row.credits_enforced)
        self.assertFalse(row.entitlements_enforced)


class MigrationGraphTests(TransactionTestCase):
    """MG-20."""

    def test_mg20_the_database_is_fully_migrated_and_the_models_match(self):
        """MG-20: **the suite's own database is the proof of the first half.**

        It was built by running every migration from zero against an empty
        database, so every data step ran and handled no rows. What that cannot
        catch is a model changed without a migration — which is what makes the
        *next* migrate-from-zero fail — so that is asserted here.
        """
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()

        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        self.assertEqual(
            [str(migration) for migration, _ in pending], [],
            "the test database is not fully migrated",
        )

        # Raises SystemExit(1) if any model has drifted from its migrations.
        call_command("makemigrations", "--check", "--dry-run", verbosity=0)
