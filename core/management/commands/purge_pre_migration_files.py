"""
Delete file-owning rows whose object keys point into the storage backend we left behind.

    python manage.py purge_pre_migration_files              # read-only report
    python manage.py purge_pre_migration_files --confirm    # destructive, opt-in

**Why this exists.** The object storage backend moved from Cloudflare R2 to Azure Blob.
The keys themselves ported unchanged -- ``t-<tenant>/<folder>/<uuid>_<name>`` is a legal
blob name -- but the *objects* did not, because the R2 data was disposable and was not
migrated. Every row in the four file-owning models therefore names a blob that does not
exist in the new container.

Those rows cannot be cleaned up by anything else already in the codebase, and it is worth
being precise about why:

- ``cleanup_pending_uploads`` sweeps only ``PENDING`` rows. These are ``COMPLETED``.
- ``audit_orphaned_objects`` walks *objects* and finds the ones with no row. This is the
  exact inverse -- rows with no object -- and it is blind to them by construction.

Left in place they are rows that 404 on every download, forever, with nothing that will
ever collect them.

**This is a one-time migration command.** Once it has run on an environment it should
find nothing, and it is safe to delete along with this docstring when the migration is
old news.

**Read-only unless you pass ``--confirm``.**

Two details that make this safe to run *after* the environment is pointed at Azure, and
unsafe to run before:

- Deletion goes through ``all_objects`` -- a plain ``Manager``, so this is a real SQL
  ``DELETE`` and does **not** run the per-instance ``delete()`` overrides that would try
  to remove the blob. Nothing here reaches out to object storage at all. Run it while
  the old R2 credentials are still live and it still would not touch R2, but there is no
  reason to take the chance.
- ``AgentCallLog.recording`` is ``SET_NULL``, so call logs survive with the reference
  cleared rather than being cascaded away with their recordings.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models.agent_call_recording import AgentCallRecording
from core.models.catalog_item_media import CatalogItemMedia
from core.models.contact_import import ContactImportFile
from core.models.knowledge_base import KBFiles

#: Every model whose ``file`` column holds an object key. Same list as
#: ``core.services.uploads.KEY_OWNING_MODELS``, restated rather than imported so that
#: adding a model there does not silently widen what this destructive command deletes.
FILE_OWNING_MODELS = [
    (KBFiles, "Knowledge Base file"),
    (AgentCallRecording, "Call recording"),
    (CatalogItemMedia, "Catalog media"),
    (ContactImportFile, "Contact import file"),
]


class Command(BaseCommand):
    help = (
        "Deletes rows whose stored object keys point at the pre-migration storage "
        "backend. Read-only by default; --confirm performs the deletion."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help=(
                "Actually delete. Without this the command only reports what it would "
                "remove."
            ),
        )
        parser.add_argument(
            "--tenant-id",
            type=str,
            default=None,
            help="Restrict to one tenant. Default: every tenant.",
        )

    def handle(self, *args, **options):
        confirm = options["confirm"]
        tenant_id = options.get("tenant_id")

        def rows(model):
            qs = model.all_objects.all()
            if tenant_id:
                qs = qs.filter(tenant_id=tenant_id)
            return qs

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "Rows owning a pre-migration object key"
                + (f" (tenant {tenant_id})" if tenant_id else "")
            )
        )

        planned = []
        for model, label in FILE_OWNING_MODELS:
            qs = rows(model)
            total = qs.count()
            with_key = qs.exclude(file="").exclude(file__isnull=True).count()
            planned.append((model, label, total, with_key))
            self.stdout.write(
                f"  {label:22} rows={total:<7} carrying an object key={with_key}"
            )

        grand_total = sum(total for _, _, total, _ in planned)

        if grand_total == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "\nNothing to purge. Either this has already run here, or the "
                    "environment never held pre-migration files."
                )
            )
            return

        if not confirm:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDry run. {grand_total} row(s) would be deleted, and every file "
                    f"they name would become unrecoverable."
                )
            )
            self.stdout.write(
                "Knowledge bases will need re-ingesting through the ETL afterwards; "
                "that is a re-upload, not a recovery."
            )
            self.stdout.write("Re-run with --confirm to proceed.")
            return

        # One transaction: a partial purge leaves an environment that is neither the old
        # state nor the new one, and works out which is which only by inspection.
        with transaction.atomic():
            for model, label, _, _ in planned:
                deleted, per_table = rows(model).delete()
                self.stdout.write(f"  {label:22} deleted={deleted:<7} {per_table}")

        self.stdout.write(
            self.style.SUCCESS(f"\nPurged {grand_total} row(s).")
        )
        self.stdout.write(
            "Call logs were kept: AgentCallLog.recording is SET_NULL, so a log whose "
            "recording went with this now simply has none."
        )
