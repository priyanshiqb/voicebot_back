"""Fill ``AgentCallLog.credits_charged`` for calls charged before the field existed.

From now on the field is written at charge time. Older rows have their charge in the
ledger and nothing on the log, so anything reading the log — a campaign report, the
dashboard — would show every historical call as never charged.

**Both ledger keys are matched, and that is the point of the command.** A charge that
drew on both wallet buckets wrote two rows, the second keyed ``{call_id}#purchased``.
Matching the bare call id alone would fill those calls with the allocated share only:
the exact undercount this field was added to stop.

What it cannot recover: a historical *free* call (a rate of 0) wrote no ledger row, so
here it is indistinguishable from a call that was never charged, and stays null. Only
calls charged after the field shipped can record ``0``.

Only ``USAGE_DEDUCTION`` rows count. ``USAGE_ADJUSTMENT`` exists as an entry type but
nothing writes it today; if something starts to, both this and the charge path need to
include it.

    python manage.py backfill_call_credits --dry-run
    python manage.py backfill_call_credits
    python manage.py backfill_call_credits --tenant <tenant uuid>
"""

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models.agent_call_log import AgentCallLog
from wallet.models import CreditLedger

#: The key a two-bucket charge gives its second ledger row (see ``_post_debit``).
PURCHASED_SUFFIX = f"#{CreditLedger.Bucket.PURCHASED}"


def call_id_for(reference_id):
    """The call a usage row belongs to.

    Strips the known suffix rather than splitting on ``#``: a provider's own call id may
    legitimately contain one, and splitting would attribute the charge to a call that
    does not exist.
    """
    if reference_id.endswith(PURCHASED_SUFFIX):
        return reference_id[: -len(PURCHASED_SUFFIX)]
    return reference_id


class Command(BaseCommand):
    help = "Fill credits_charged on call logs charged before the field existed."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Only this tenant's call logs.")
        parser.add_argument("--batch-size", type=int, default=500,
                            help="Call logs per ledger query (default 500).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would change, then roll back.")

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        dry_run = options["dry_run"]

        # Zero-duration calls are never rated, so the ledger holds nothing for them.
        queryset = (
            AgentCallLog.objects.filter(credits_charged__isnull=True)
            .exclude(duration_seconds=0)
            .order_by("pk")
        )
        if options["tenant"]:
            queryset = queryset.filter(tenant_id=options["tenant"])

        scanned = filled = split = 0
        last_pk = None

        while True:
            page = queryset if last_pk is None else queryset.filter(pk__gt=last_pk)
            rows = list(page.values_list("pk", "provider_call_id")[:batch_size])
            if not rows:
                break
            # Keyset rather than offset: rows filled in this pass drop out of the filter,
            # and an offset would then skip a page of unfilled rows every time they did.
            last_pk = rows[-1][0]
            scanned += len(rows)

            pk_for = {call_id: pk for pk, call_id in rows}
            keys = [*pk_for, *(f"{call_id}{PURCHASED_SUFFIX}" for call_id in pk_for)]

            totals = defaultdict(int)
            parts = defaultdict(int)
            for reference_id, amount in CreditLedger.all_objects.filter(
                entry_type=CreditLedger.EntryType.USAGE_DEDUCTION,
                reference_id__in=keys,
            ).values_list("reference_id", "amount"):
                call_id = call_id_for(reference_id)
                totals[call_id] += abs(amount)
                parts[call_id] += 1

            if not totals:
                continue

            # `updated_at` is deliberately left alone: a backfill that bumped it would
            # reorder every "recently updated" list by when a script happened to run.
            with transaction.atomic():
                AgentCallLog.objects.bulk_update(
                    [
                        AgentCallLog(pk=pk_for[call_id], credits_charged=total)
                        for call_id, total in totals.items()
                    ],
                    ["credits_charged"],
                )
                if dry_run:
                    transaction.set_rollback(True)

            filled += len(totals)
            split += sum(1 for count in parts.values() if count > 1)

        self.stdout.write(self.style.SUCCESS(
            f"{filled} call log(s) filled, {split} of them from a charge split across both "
            f"buckets; {scanned - filled} had no charge in the ledger and were left null."
        ))
        if split:
            self.stdout.write(
                f"Those {split} are the rows a join on the bare call id undercounts."
            )
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN: rolled back."))
