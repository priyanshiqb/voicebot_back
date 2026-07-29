"""
Remove upload records whose presigned upload was never completed.

`request-upload-url` now reserves the row up front so the object key can stay
server-side. If the client never PUTs the file, or never calls confirm-upload, that row
lingers in PENDING. This sweeps them once they are old enough that no upload could still
be in flight.
"""

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models.agent_call_recording import AgentCallRecording
from core.models.knowledge_base import KBFiles

logger = logging.getLogger(__name__)

# (model, human label)
SWEPT = [
    (KBFiles, "Knowledge Base file"),
    (AgentCallRecording, "Call recording"),
]


class Command(BaseCommand):
    help = (
        "Deletes upload records still PENDING after a cutoff, i.e. presigned uploads "
        "the client abandoned. Contact import files are handled by the import session "
        "TTL sweep instead."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than-hours",
            type=int,
            default=24,
            help="Only sweep records older than this (default 24), so in-flight uploads are safe.",
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
        cutoff = timezone.now() - timezone.timedelta(hours=options["older_than_hours"])
        dry_run = options["dry_run"]
        tenant_id = options["tenant_id"]

        for model, label in SWEPT:
            queryset = model.all_objects.filter(
                upload_status=model.UploadStatus.PENDING, created_at__lte=cutoff
            )
            if tenant_id:
                queryset = queryset.filter(tenant_id=tenant_id)

            total = queryset.count()
            if total == 0:
                self.stdout.write(self.style.SUCCESS(f"{label}: nothing to sweep."))
                continue

            if dry_run:
                self.stdout.write(
                    self.style.NOTICE(f"{label}: would delete {total} pending record(s).")
                )
                for record in queryset[:20]:
                    self.stdout.write(f"    id={record.id}  tenant={record.tenant_id}")
                continue

            deleted = 0
            for record in queryset:
                try:
                    # delete() also removes the blob where the model defines it; an
                    # abandoned upload usually has no object, which is tolerated.
                    record.delete()
                    deleted += 1
                except Exception as exc:  # noqa: BLE001 — one bad row must not stop the sweep
                    logger.warning("Could not delete pending %s %s: %s", label, record.id, exc)
                    self.stdout.write(
                        self.style.ERROR(f"    failed to delete {record.id}: {exc}")
                    )

            self.stdout.write(
                self.style.SUCCESS(f"{label}: deleted {deleted} of {total} pending record(s).")
            )
