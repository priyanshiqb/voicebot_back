"""Section 30 of the billing test plan — CN-01 to CN-12.

Two calls must not be able to spend the same credit.

**The trap.** Django's ``TestCase`` wraps every test in a transaction that is rolled
back at the end, so two threads cannot see each other's uncommitted data — the race
these tests exist to exercise *cannot happen* there. And a second ``SELECT … FOR
UPDATE`` on the same row **inside the same transaction does not block**: it is the same
transaction, so it succeeds immediately. Any interleaving test must use two real
database connections, or it is testing nothing at all.

**The fix.** Do not run two threads and hope one loses — *choose* the interleaving with
``threading.Event``, so the same sequence happens on every run, on every machine. Every
wait carries a timeout, which turns a genuine deadlock into a clear failure rather than
a hung suite.

Three techniques, cheapest last:

* **A** — deterministic hand-off between two connections (CN-04 to CN-09).
* **B** — hold the lock and prove a second connection cannot take it (CN-01).
* **C** — assert the ``FOR UPDATE`` clause is in the SQL (CN-02, CN-03). Runs under
  plain ``TestCase`` and catches the regression most likely to happen: somebody
  removing ``select_for_update()`` while refactoring.
"""

import threading

from django.db import OperationalError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from onboard.models import RazorpayWebhookLog, Subscription, Transaction
from onboard.services import subscriptions as subscription_service
from onboard.tasks import process_billing_cycle
from onboard.tests.billing_factories import (
    WEBHOOK_URL,
    assert_invariant,
    make_plan,
    make_tenant,
    make_transaction,
)
from wallet.models import CreditLedger, CreditRate, Wallet
from wallet.services import wallet as wallet_service

Bucket = CreditLedger.Bucket
EntryType = CreditLedger.EntryType
Operation = CreditRate.Operation
Status = Subscription.SubscriptionStatusChoices

#: Long enough that a slow machine does not fail, short enough that a real deadlock
#: is reported in seconds rather than hanging the suite.
WAIT = 10


class Worker(threading.Thread):
    """A thread with its own database connection, which it always closes.

    **Django opens a connection per thread and ``TransactionTestCase`` will not tidy
    them up.** A leaked connection is what makes the *next* test fail mysteriously,
    so closing is the thread's own responsibility and CN-11 checks it happened.
    """

    def __init__(self, name, work):
        super().__init__(name=name, daemon=True)
        self.work = work
        self.result = None
        self.error = None
        self.closed = False

    def run(self):
        try:
            self.result = self.work()
        except Exception as exc:  # surfaced by the assertions below, never swallowed
            self.error = exc
        finally:
            connection.close()
            self.closed = True


class InterleavingTestCase(TransactionTestCase):
    """A tenant whose wallet is about to be raced for."""

    def setUp(self):
        self.ctx = make_tenant("race")
        self.tenant = self.ctx.tenant
        self.plan = make_plan(
            "race-plan",
            rates={Operation.CONVERSATION_HIGH_LATENCY: 1},
            allocated_credits=0,
        )
        wallet_service.set_plan_context(self.tenant, self.plan)

    def fund(self, credits):
        wallet_service.grant(
            self.tenant, entry_type=EntryType.CYCLE_ALLOCATION,
            amount=credits, reference_id=f"race-seed-{credits}",
        )

    def charge(self, reference, seconds=60):
        return wallet_service.consume(
            self.tenant,
            operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=seconds,
            reference_id=reference,
        )

    @property
    def wallet(self):
        return Wallet.objects.get(tenant=self.tenant)

    def run_workers(self, *workers):
        """Start every worker, join with a timeout, and surface any exception."""
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=WAIT * 3)
            self.assertFalse(
                worker.is_alive(),
                f"{worker.name} never finished — the interleaving deadlocked",
            )
        for worker in workers:
            if worker.error is not None:
                raise AssertionError(f"{worker.name} raised: {worker.error}")
        return workers

    def handoff(self, first_work, second_work):
        """Run ``first_work`` holding its lock while ``second_work`` waits on it.

        The events make the order deterministic: B never begins until A holds the
        lock, and A never commits until B has been released to try for it.
        """
        a_has_lock = threading.Event()
        a_may_finish = threading.Event()

        def a():
            with transaction.atomic():
                outcome = first_work()
                a_has_lock.set()
                a_may_finish.wait(timeout=WAIT)
            return outcome

        def b():
            if not a_has_lock.wait(timeout=WAIT):
                raise AssertionError("A never signalled that it held the lock")
            return second_work()

        worker_a = Worker("A", a)
        worker_b = Worker("B", b)

        worker_a.start()
        self.assertTrue(
            a_has_lock.wait(timeout=WAIT), "A never took the lock"
        )
        worker_b.start()
        a_may_finish.set()

        for worker in (worker_a, worker_b):
            worker.join(timeout=WAIT * 3)
            self.assertFalse(
                worker.is_alive(), f"{worker.name} deadlocked"
            )
            if worker.error is not None:
                raise AssertionError(f"{worker.name} raised: {worker.error}")

        return worker_a, worker_b


class LockProofTests(InterleavingTestCase):
    """CN-01 — technique B."""

    def test_cn01_a_second_connection_cannot_take_a_held_row_lock(self):
        """CN-01: **the lock proved directly, rather than by its consequences.**

        ``nowait=True`` turns "wait for the lock" into "raise immediately", which
        removes the timing element entirely.
        """
        self.fund(10)
        wallet_id = self.wallet.pk
        def probe():
            # Its own transaction: select_for_update outside one is refused by
            # Django before it ever reaches the database.
            with transaction.atomic():
                return Wallet.objects.select_for_update(nowait=True).get(
                    pk=wallet_id
                )

        blocked = Worker("nowait", probe)

        with transaction.atomic():
            Wallet.objects.select_for_update().get(pk=wallet_id)
            blocked.start()
            blocked.join(timeout=WAIT)

        self.assertFalse(blocked.is_alive(), "the nowait probe hung")
        self.assertIsInstance(
            blocked.error, OperationalError,
            "a second connection took a lock that was already held",
        )


class ConsumeInterleavingTests(InterleavingTestCase):
    """CN-04 to CN-06, CN-10 — technique A on the wallet."""

    def test_cn04_only_one_of_two_charges_lands_on_the_last_credit(self):
        """CN-04: **without the row lock both read a sufficient balance and both
        spend it**, and the wallet ends up owing credits it never sold."""
        self.fund(1)

        worker_a, worker_b = self.handoff(
            lambda: self.charge("race-a"),
            lambda: self.charge("race-b"),
        )

        landed = [w for w in (worker_a, worker_b) if w.result is not None]
        self.assertEqual(
            len(landed), 1, "exactly one charge may land on a one-credit wallet"
        )
        wallet = assert_invariant(self, self.tenant, msg="after the race")
        self.assertEqual(wallet.balance, 0)
        self.assertEqual(
            CreditLedger.all_objects.filter(
                tenant=self.tenant, entry_type=EntryType.USAGE_DEDUCTION
            ).count(),
            1,
        )

    def test_cn05_two_affordable_charges_both_land(self):
        """CN-05: the lock serialises, it does not refuse. A guard that turned
        contention into a lost charge would be quietly dropping revenue."""
        self.fund(10)

        worker_a, worker_b = self.handoff(
            lambda: self.charge("race-both-a"),
            lambda: self.charge("race-both-b"),
        )

        self.assertIsNotNone(worker_a.result)
        self.assertIsNotNone(worker_b.result)
        wallet = assert_invariant(self, self.tenant, msg="after two charges")
        self.assertEqual(wallet.balance, 8)

    def test_cn06_a_grant_and_a_charge_interleaved_both_apply(self):
        """CN-06: **a renewal payment landing while a call is being charged.**

        Both are real movements; neither may be lost, and the ledger has to end up
        explaining the balance either way round.
        """
        self.fund(5)

        worker_a, worker_b = self.handoff(
            lambda: wallet_service.grant(
                self.tenant, entry_type=EntryType.MANUAL_TOP_UP, amount=100,
                bucket=Bucket.PURCHASED, reference_id="race-grant",
            ),
            lambda: self.charge("race-grant-then-charge"),
        )

        self.assertIsNotNone(worker_a.result)
        self.assertIsNotNone(worker_b.result)
        wallet = assert_invariant(self, self.tenant, msg="after grant and charge")
        self.assertEqual(wallet.balance, 104)
        self.assertEqual(wallet.purchased_balance, 100)


class SubscriptionInterleavingTests(InterleavingTestCase):
    """CN-07 to CN-09 — technique A beyond the wallet."""

    def test_cn07_two_activations_for_one_tenant_produce_one_subscription(self):
        """CN-07: **the browser-confirmed payment and the webhook both complete
        onboarding, and both can win.**

        The one-active-per-tenant constraint would turn a lost race into an
        IntegrityError on a request the customer has already paid for.
        """
        worker_a, worker_b = self.handoff(
            lambda: subscription_service.activate(self.tenant, self.plan),
            lambda: subscription_service.activate(self.tenant, self.plan),
        )

        self.assertIsNotNone(worker_a.result)
        self.assertIsNotNone(worker_b.result)
        self.assertEqual(worker_a.result.pk, worker_b.result.pk)
        self.assertEqual(
            Subscription.objects.filter(
                tenant=self.tenant, status=Status.ACTIVE
            ).count(),
            1,
        )
        assert_invariant(self, self.tenant, msg="after two activations")

    def test_cn08_two_billing_sweeps_expire_a_subscription_once(self):
        """CN-08: **the re-check under the lock.**

        Both workers scan the same lapsed row. The second must find it already
        settled and leave it alone, rather than forfeiting a second time.
        """
        subscription = subscription_service.activate(self.tenant, self.plan)
        self.fund(500)
        Subscription.objects.filter(pk=subscription.pk).update(
            start_date=timezone.now() - timezone.timedelta(days=32),
            end_date=timezone.now() - timezone.timedelta(days=2),
        )

        worker_a, worker_b = self.handoff(
            process_billing_cycle, process_billing_cycle
        )

        expired = worker_a.result["expired"] + worker_b.result["expired"]
        self.assertEqual(expired, 1, "the subscription was expired twice")
        self.assertEqual(
            CreditLedger.all_objects.filter(
                tenant=self.tenant, entry_type=EntryType.EXPIRY_FORFEIT
            ).count(),
            1,
        )
        assert_invariant(self, self.tenant, msg="after two sweeps")

    def test_cn09_the_same_webhook_on_two_connections_applies_once(self):
        """CN-09: Razorpay can have two deliveries of one event in flight at the
        same moment. ``event_id`` is unique, so the second short-circuits."""
        subscription = subscription_service.activate(self.tenant, self.plan)
        order = make_transaction(
            self.tenant, self.plan,
            status=Transaction.TransactionStatusChoices.PENDING,
            transaction_type=Transaction.TransactionTypeChoices.TOP_UP,
            credits_purchased=500,
            subscription=subscription,
        )
        payload = {
            "id": "evt_concurrent",
            "event": "payment.captured",
            "payload": {"payment": {"entity": {
                "id": "pay_concurrent", "order_id": order.razorpay_order_id}}},
        }

        def deliver():
            return APIClient().post(WEBHOOK_URL, payload, format="json")

        worker_a, worker_b = self.handoff(deliver, deliver)

        self.assertEqual(worker_a.result.status_code, 200)
        self.assertEqual(worker_b.result.status_code, 200)
        self.assertEqual(
            RazorpayWebhookLog.objects.filter(event_id="evt_concurrent").count(), 1
        )
        wallet = assert_invariant(self, self.tenant, msg="after a duplicate webhook")
        self.assertEqual(wallet.purchased_balance, 500)


class HarnessTests(InterleavingTestCase):
    """CN-11 to CN-12 — properties of the technique itself."""

    def test_cn11_every_worker_closes_its_connection(self):
        """CN-11: **a leaked connection is what makes the next test fail
        mysteriously.** Closing is in ``finally``, so it happens even when the work
        raises."""
        self.fund(10)

        worker_a, worker_b = self.handoff(
            lambda: self.charge("race-close-a"),
            lambda: self.charge("race-close-b"),
        )

        self.assertTrue(worker_a.closed)
        self.assertTrue(worker_b.closed)

        exploding = Worker("boom", lambda: 1 / 0)
        exploding.start()
        exploding.join(timeout=WAIT)
        self.assertTrue(
            exploding.closed, "a failing worker left its connection open"
        )

    def test_cn12_a_partner_that_never_signals_fails_rather_than_hangs(self):
        """CN-12: **the timeout is what stops a deadlock hanging CI.**

        The waiting worker gives up after ``WAIT`` seconds and reports it, which is
        a test failure someone can read — not a job that runs until the runner
        kills it.
        """
        never_set = threading.Event()

        def waits_forever():
            if not never_set.wait(timeout=0.2):
                raise AssertionError("partner never signalled")
            return "unreachable"

        worker = Worker("lonely", waits_forever)
        worker.start()
        worker.join(timeout=WAIT)

        self.assertFalse(worker.is_alive(), "the waiting worker hung")
        self.assertIsInstance(worker.error, AssertionError)
        self.assertIn("never signalled", str(worker.error))


class LockClauseTests(TestCase):
    """CN-02 to CN-03, CN-10 — technique C, at normal speed."""

    def setUp(self):
        self.ctx = make_tenant("lock-clause")
        self.tenant = self.ctx.tenant
        self.plan = make_plan(
            "lock-clause-plan",
            rates={Operation.CONVERSATION_HIGH_LATENCY: 1},
            allocated_credits=0,
        )
        wallet_service.set_plan_context(self.tenant, self.plan)

    def locked_statements(self, work):
        with CaptureQueriesContext(connection) as queries:
            work()
        return [
            entry["sql"] for entry in queries.captured_queries
            if "FOR UPDATE" in entry["sql"]
        ]

    def test_cn02_consume_locks_the_wallet_row(self):
        """CN-02: **the regression most likely to happen** — somebody removing
        ``select_for_update()`` while refactoring, with every test still green
        because nothing else exercises contention."""
        wallet_service.grant(
            self.tenant, entry_type=EntryType.CYCLE_ALLOCATION, amount=100,
            reference_id="lock-clause-seed",
        )

        locked = self.locked_statements(
            lambda: wallet_service.consume(
                self.tenant,
                operation=Operation.CONVERSATION_HIGH_LATENCY,
                quantity=60,
                reference_id="lock-clause-charge",
            )
        )

        self.assertEqual(len(locked), 1)
        self.assertIn("wallet_wallet", locked[0])

    def test_cn03_grant_locks_the_wallet_row(self):
        """CN-03: a grant is as much a read-modify-write as a charge, and two
        renewals landing together would otherwise lose one."""
        locked = self.locked_statements(
            lambda: wallet_service.grant(
                self.tenant, entry_type=EntryType.MANUAL_TOP_UP, amount=50,
                bucket=Bucket.PURCHASED, reference_id="lock-clause-grant",
            )
        )

        self.assertEqual(len(locked), 1)
        self.assertIn("wallet_wallet", locked[0])

    def test_cn10_the_invariant_holds_after_a_sequential_mix(self):
        """CN-10: the same assertion the interleaving cases end on, without the
        cost — so a broken bucket split is caught by the fast suite too."""
        wallet_service.grant(
            self.tenant, entry_type=EntryType.CYCLE_ALLOCATION, amount=100,
            reference_id="cn10-allocated",
        )
        wallet_service.grant(
            self.tenant, entry_type=EntryType.MANUAL_TOP_UP, amount=50,
            bucket=Bucket.PURCHASED, reference_id="cn10-purchased",
        )
        wallet_service.consume(
            self.tenant, operation=Operation.CONVERSATION_HIGH_LATENCY,
            quantity=7200, reference_id="cn10-charge",
        )

        wallet = assert_invariant(self, self.tenant, msg="after a mixed sequence")
        self.assertEqual(wallet.allocated_balance, 0)
        self.assertEqual(wallet.purchased_balance, 30)
