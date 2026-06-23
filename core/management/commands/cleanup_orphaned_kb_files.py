import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models.knowledge_base import KBFiles

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Cleans up orphaned KBFiles that have a tenant but do not belong to any KnowledgeBase."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Dry run to see how many files would be deleted without actually deleting them.',
        )
        parser.add_argument(
            '--older-than-hours',
            type=int,
            default=24,
            help='Only delete orphaned files older than this many hours to prevent deleting files currently being uploaded (default 24).',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        older_than_hours = options['older_than_hours']

        cutoff_time = timezone.now() - timezone.timedelta(hours=older_than_hours)

        # Find orphaned KBFiles (tenant is not null, but no knowledge bases attached)
        # We also check if it's older than the cutoff time so we don't delete files that a user JUST uploaded and hasn't saved the KB yet.
        orphaned_files = KBFiles.objects.filter(
            tenant__isnull=False,
            knowledge_bases__isnull=True,
            created_at__lte=cutoff_time
        )

        total_orphaned = orphaned_files.count()

        if total_orphaned == 0:
            self.stdout.write(self.style.SUCCESS(f"No orphaned KB files older than {older_than_hours} hours found to clean up."))
            return

        self.stdout.write(self.style.WARNING(f"Found {total_orphaned} orphaned KB file(s) older than {older_than_hours} hours."))

        if dry_run:
            self.stdout.write(self.style.NOTICE("DRY RUN: Skipping actual deletion."))
            for f in orphaned_files:
                self.stdout.write(f" - Would delete: {f.id} ({f.original_name}) from tenant {f.tenant_id} (Created at: {f.created_at.strftime('%Y-%m-%d %H:%M:%S')})")
            return

        deleted_count = 0
        for f in orphaned_files:
            try:
                # This calls the custom delete() method on KBFiles which removes the physical S3 object as well
                f.delete()
                deleted_count += 1
                self.stdout.write(self.style.SUCCESS(f"Deleted {f.id} ({f.original_name}) (Created at: {f.created_at.strftime('%Y-%m-%d %H:%M:%S')})"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Failed to delete {f.id}: {str(e)}"))

        self.stdout.write(self.style.SUCCESS(f"Successfully cleaned up {deleted_count} orphaned KB file(s)."))
