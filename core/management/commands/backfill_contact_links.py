import logging

from django.core.management.base import BaseCommand

from core.services import contact_resolution

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Populates the `contact` foreign key on existing call logs and booking "
        "participants by matching their phone numbers to Contacts. Kept out of the "
        "schema migration so a slow backfill never blocks a deploy. Safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            type=str,
            default=None,
            help="Restrict the backfill to a single tenant.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Rows per bulk_update (default 500).",
        )
        parser.add_argument(
            "--call-logs-only",
            action="store_true",
            help="Backfill call logs and skip booking participants.",
        )
        parser.add_argument(
            "--participants-only",
            action="store_true",
            help="Backfill booking participants and skip call logs.",
        )

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        batch_size = options["batch_size"]
        do_calls = not options["participants_only"]
        do_participants = not options["call_logs_only"]

        if do_calls:
            linked = contact_resolution.backfill_call_logs(
                tenant_id=tenant_id, batch_size=batch_size
            )
            self.stdout.write(
                self.style.SUCCESS(f"Linked {linked} call log(s) to contacts.")
            )

        if do_participants:
            linked = contact_resolution.backfill_participants(
                tenant_id=tenant_id, batch_size=batch_size
            )
            self.stdout.write(
                self.style.SUCCESS(f"Linked {linked} booking participant(s) to contacts.")
            )
