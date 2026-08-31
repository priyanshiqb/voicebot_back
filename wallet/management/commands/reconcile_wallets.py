from django.core.management.base import BaseCommand

from wallet.models import Wallet
from wallet.services import reconciliation


class Command(BaseCommand):
    """Assert that every wallet's balance matches its ledger.

        SUM(CreditLedger.amount) == Wallet.balance          (and per bucket)

    **Reports by default, never repairs.** Silently correcting drift hides the bug
    that caused it — and the balance is the symptom, not the disease. `--fix` exists
    for a deliberate operator decision after the cause is understood, and it writes an
    ADMIN_ADJUSTMENT rather than editing the balance, so the correction is itself
    auditable.

        python manage.py reconcile_wallets
        python manage.py reconcile_wallets --fix --reason "TICKET-1234"
    """

    help = "Check every wallet's balance against its credit ledger."

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true",
                            help="Post a correcting ADMIN_ADJUSTMENT for each drift.")
        parser.add_argument("--reason", default="",
                            help="Ticket reference recorded on any correction.")

    def handle(self, *args, **options):
        drifted = 0
        checked = 0

        # Two queries for the whole table, not three per wallet. The readable version
        # was 3n queries — fine at seventeen wallets, unusable at a hundred thousand,
        # and this job is a full scan by definition.
        #
        # The comparison itself lives in `wallet.services.reconciliation` so the admin
        # console asks the same question of one wallet and cannot get a different
        # answer. Passing `sums` in is what preserves the two-query property.
        totals = reconciliation.bulk_ledger_sums()

        for wallet in Wallet.objects.select_related("tenant").iterator():
            checked += 1
            sums = totals.get(wallet.pk, {})
            total = reconciliation.ledger_total(wallet, sums=sums)
            problems = reconciliation.check(wallet, sums=sums)

            if not problems:
                continue

            drifted += 1
            detail = "; ".join(p["message"] for p in problems)
            self.stderr.write(self.style.ERROR(
                f"DRIFT wallet={wallet.pk} tenant={wallet.tenant} :: {detail}"
            ))

            if options["fix"]:
                from django.utils import timezone

                from wallet.services import wallet as svc

                delta = total - wallet.balance
                if delta:
                    stamp = timezone.now().strftime("%Y%m%d%H%M%S")
                    svc.adjust(
                        wallet.tenant_id,
                        amount=delta,
                        reference_id=f"reconcile:{wallet.pk}:{stamp}",
                        notes=f"Reconciliation. {options['reason']}".strip(),
                    )
                    self.stdout.write(self.style.WARNING(
                        f"  corrected by {delta:+d} credits"
                    ))

        if drifted:
            self.stderr.write(self.style.ERROR(
                f"\n{drifted} of {checked} wallet(s) drifted."
            ))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS(
            f"OK: {checked} wallet(s) reconcile against their ledger."
        ))
