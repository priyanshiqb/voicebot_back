"""
Remove upload records whose presigned upload was never completed.

Thin wrapper over ``core.services.uploads.sweep_pending_uploads`` — the same code the
nightly beat task runs, so an operator running this by hand and the scheduler cannot
drift apart. See that module for what is swept and why.
"""

from django.core.management.base import BaseCommand

from core.services import uploads as upload_service


class Command(BaseCommand):
    help = (
        "Deletes upload records still PENDING after a cutoff, i.e. presigned uploads "
        "the client abandoned, along with the objects they left in the bucket. "
        "Contact import files are handled by the import session TTL sweep instead."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than-hours",
            type=int,
            default=None,
            help=(
                "Only sweep records older than this, so in-flight uploads are safe. "
                "Defaults to settings.PENDING_UPLOAD_TTL_HOURS."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting it.",
        )
        parser.add_argument(
            "--tenant-id", type=str, default=None, help="Restrict to one tenant."
        )

    def handle(self, *args, **options):
        results = upload_service.sweep_pending_uploads(
            older_than_hours=options["older_than_hours"],
            tenant_id=options["tenant_id"],
            dry_run=options["dry_run"],
        )

        for result in results:
            label = result["label"]

            if not result["matched"]:
                self.stdout.write(self.style.SUCCESS(f"{label}: nothing to sweep."))
                continue

            if options["dry_run"]:
                self.stdout.write(
                    self.style.NOTICE(
                        f"{label}: would delete {result['matched']} pending record(s)."
                    )
                )
                for record_id in result["sample_ids"]:
                    self.stdout.write(f"    id={record_id}")
                continue

            self.stdout.write(
                self.style.SUCCESS(
                    f"{label}: deleted {result['deleted']} of {result['matched']} "
                    "pending record(s)."
                )
            )
            if result["failed"]:
                self.stdout.write(
                    self.style.ERROR(
                        f"    {result['failed']} could not be deleted — see the log."
                    )
                )

        return str(sum(result["deleted"] for result in results))
