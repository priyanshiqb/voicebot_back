from django.core.management.base import BaseCommand
from django.db.models import Sum

from wallet.models import CreditLedger, Wallet


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

        for wallet in Wallet.objects.select_related("tenant").iterator():
            checked += 1
            sums = wallet.entries.aggregate(total=Sum("amount"))
            total = sums["total"] or 0
            allocated = wallet.entries.filter(
                bucket=CreditLedger.Bucket.ALLOCATED).aggregate(s=Sum("amount"))["s"] or 0
            purchased = wallet.entries.filter(
                bucket=CreditLedger.Bucket.PURCHASED).aggregate(s=Sum("amount"))["s"] or 0

            problems = []
            if total != wallet.balance:
                problems.append(f"balance {wallet.balance} vs ledger {total}")
            if allocated != wallet.allocated_balance:
                problems.append(f"allocated {wallet.allocated_balance} vs ledger {allocated}")
            if purchased != wallet.purchased_balance:
                problems.append(f"purchased {wallet.purchased_balance} vs ledger {purchased}")

            if not problems:
                continue

            drifted += 1
            self.stderr.write(self.style.ERROR(
                f"DRIFT wallet={wallet.pk} tenant={wallet.tenant} :: {'; '.join(problems)}"
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
