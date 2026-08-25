"""Give existing subscriptions the wallet state that activate() would have created.

Every subscription made before ``subscriptions.activate()`` existed was a bare row:
no rate card on the wallet, no cycle allocation. Those tenants cannot be charged for a
call because nothing knows what a call costs them.

Split into two deliberately:

* **Stamping the rate card is configuration.** It is unambiguously correct — a wallet
  should always carry the prices of the plan its tenant is on — so it runs by default.
* **Granting the cycle's credits is money.** Whether a historical subscription is owed
  an allocation it never received is a business decision, not a data-repair one, so it
  needs ``--grant-allocations`` to say so out loud.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from onboard.models import Subscription
from wallet.models import CreditLedger, Wallet
from wallet.services import wallet as wallet_service


class Command(BaseCommand):
    help = "Stamp rate cards (and optionally grant allocations) on existing subscriptions."

    def add_arguments(self, parser):
        parser.add_argument("--grant-allocations", action="store_true",
                            help="Also grant the plan's cycle credits where none were ever issued.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change, then roll back.")

    def handle(self, *args, **options):
        grant = options["grant_allocations"]
        dry_run = options["dry_run"]

        # Live tenants only. A soft-deleted tenant keeps its subscription rows, but
        # stamping a wallet nobody can reach just makes the audit harder to read —
        # and this database has more deleted tenants than live ones.
        subscriptions = (
            Subscription.objects.filter(
                status=Subscription.SubscriptionStatusChoices.ACTIVE,
                tenant__is_deleted=False,
            )
            .select_related("plan", "tenant")
            .order_by("tenant__name")
        )

        stamped = granted = skipped = failed = 0

        with transaction.atomic():
            for subscription in subscriptions:
                try:
                    wallet = Wallet.for_tenant(subscription.tenant_id)

                    if wallet.rate_snapshot:
                        skipped += 1
                    else:
                        wallet_service.set_plan_context(subscription.tenant_id, subscription.plan)
                        stamped += 1
                        self.stdout.write(
                            f"  stamped {subscription.tenant.name[:26]:28} {subscription.plan.code}"
                        )

                    if grant:
                        allocation = subscription.rules.get("allocated_credits") or 0
                        # Keyed on the subscription so a re-run cannot double-grant,
                        # and distinct from any real payment reference.
                        reference = f"backfill:{subscription.pk}"
                        if allocation > 0 and not CreditLedger.all_objects.filter(
                            entry_type=CreditLedger.EntryType.CYCLE_ALLOCATION,
                            reference_id=reference,
                        ).exists():
                            wallet_service.grant(
                                subscription.tenant_id,
                                entry_type=CreditLedger.EntryType.CYCLE_ALLOCATION,
                                amount=allocation,
                                reference_id=reference,
                                subscription=subscription,
                                notes="Backfilled allocation for a pre-service subscription",
                            )
                            granted += 1
                            self.stdout.write(
                                f"  granted {allocation:>6} credits to "
                                f"{subscription.tenant.name[:26]}"
                            )

                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    self.stderr.write(self.style.ERROR(
                        f"  FAILED {subscription.tenant.name}: {exc}"
                    ))

            if dry_run:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("\nDRY RUN: rolled back."))

        self.stdout.write(self.style.SUCCESS(
            f"\n{stamped} stamped, {granted} allocation(s) granted, "
            f"{skipped} already had a rate card, {failed} failed."
        ))
        if not grant and stamped:
            self.stdout.write(
                "Credits were not granted. Re-run with --grant-allocations if these "
                "subscriptions are owed the cycle credits they never received."
            )
