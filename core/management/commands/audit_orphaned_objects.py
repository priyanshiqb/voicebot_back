"""
Find objects in the bucket that no database row accounts for.

    python manage.py audit_orphaned_objects
    python manage.py audit_orphaned_objects --tenant-id <uuid> --verbose
    python manage.py audit_orphaned_objects --delete          # destructive, opt-in

**Why this exists rather than an S3 lifecycle rule.** The obvious way to bound storage
growth is a lifecycle rule expiring a staging prefix. This bucket's keys have no staging
prefix — an object lands on its final key at upload time, as
``t-<tenant>/kb_files/<uuid>_name.pdf`` — so a prefix rule would expire *confirmed*
files along with abandoned ones. Introducing one means changing the key layout and
copying every object on confirm, which is a bigger and riskier change than the problem
warrants while ``cleanup_pending_uploads`` already collects the abandoned rows.

What that sweep cannot see is the case this command exists for: an object with **no row
at all**. It walks rows and deletes their objects; nothing walks objects. So anything
left by a hard delete predating the ``delete()`` overrides, by a legacy upload path, or
by a request that wrote to storage and then failed before saving its row, is invisible
to the database and will never be collected. This is the only thing that finds it.

**Read-only unless you pass ``--delete``.** Deleting is judged from the *absence* of a
row, so a bug in the query is a bug that destroys customer files. Run it, read the
output, then decide.
"""

from django.core.management.base import BaseCommand, CommandError

from core.services import uploads as upload_service
from tenant.models.Tenant import Tenant
from utils.files.sizes import format_size_limit
from utils.s3_storage.service import S3StorageService


class Command(BaseCommand):
    help = (
        "Reports objects in the storage bucket that no database row references. "
        "Read-only by default; --delete removes them."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            type=str,
            default=None,
            help="Restrict to one tenant. Default: every tenant.",
        )
        parser.add_argument(
            "--older-than-hours",
            type=int,
            default=None,
            help=(
                "Ignore objects newer than this, since one written moments ago may "
                "belong to a row about to be created. Defaults to "
                "settings.PENDING_UPLOAD_TTL_HOURS."
            ),
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Actually delete what is found. Without this the command only reports.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="List every orphan instead of the first ten per tenant.",
        )

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        delete = options["delete"]
        verbose = options["verbose"]

        tenants = Tenant.objects.all()
        if tenant_id:
            tenants = tenants.filter(id=tenant_id)
            if not tenants.exists():
                raise CommandError(f"No tenant with id {tenant_id}.")

        storage = S3StorageService()
        total_orphans = 0
        total_bytes = 0
        total_deleted = 0

        for tenant in tenants:
            try:
                orphans = upload_service.find_orphaned_objects(
                    tenant.id,
                    older_than_hours=options["older_than_hours"],
                    storage=storage,
                )
            except Exception as exc:  # noqa: BLE001 — one bad tenant must not stop the audit
                self.stdout.write(
                    self.style.ERROR(f"{tenant.name}: could not be audited — {exc}")
                )
                continue

            if not orphans:
                self.stdout.write(
                    self.style.SUCCESS(f"{tenant.name}: no orphaned objects.")
                )
                continue

            tenant_bytes = sum(o["size"] for o in orphans)
            total_orphans += len(orphans)
            total_bytes += tenant_bytes

            self.stdout.write(
                self.style.WARNING(
                    f"{tenant.name}: {len(orphans)} orphaned object(s), "
                    f"{format_size_limit(tenant_bytes)}."
                )
            )
            shown = orphans if verbose else orphans[:10]
            for obj in shown:
                self.stdout.write(
                    f"    {obj['key']}  ({format_size_limit(obj['size'])}, "
                    f"modified {obj['last_modified']})"
                )
            if not verbose and len(orphans) > len(shown):
                self.stdout.write(
                    f"    ... and {len(orphans) - len(shown)} more (use --verbose)"
                )

            if delete:
                for obj in orphans:
                    try:
                        storage.delete_blob_direct(obj["key"])
                        total_deleted += 1
                    except Exception as exc:  # noqa: BLE001
                        self.stdout.write(
                            self.style.ERROR(f"    failed to delete {obj['key']}: {exc}")
                        )

        self.stdout.write("")
        if not total_orphans:
            self.stdout.write(
                self.style.SUCCESS("No orphaned objects. Storage matches the database.")
            )
        elif delete:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Deleted {total_deleted} of {total_orphans} orphaned object(s), "
                    f"reclaiming up to {format_size_limit(total_bytes)}."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"{total_orphans} orphaned object(s) holding "
                    f"{format_size_limit(total_bytes)}. Nothing was deleted — re-run "
                    f"with --delete once you have read the list above."
                )
            )

        return str(total_orphans)
