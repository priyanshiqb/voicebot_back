"""
Blast-radius audit for the cross-tenant confirm-upload hole.

Before the fix, `confirm-upload` accepted any object key the client sent and verified
only that the object existed — never that it belonged to the caller's tenant. A caller
who knew another tenant's key could confirm it into their own record and then read the
file through the presigned GET URL.

This command finds records whose stored object key sits outside their own tenant's
prefix. Read-only: it reports, it does not change anything.

    python manage.py audit_upload_key_ownership
    python manage.py audit_upload_key_ownership --verbose

A non-empty result is an incident with disclosure obligations, not a bug report.
"""

from django.core.management.base import BaseCommand

from core.models.agent_call_recording import AgentCallRecording
from core.models.catalog_item_media import CatalogItemMedia
from core.models.contact_import import ContactImportFile
from core.models.knowledge_base import KBFiles
from utils.querysets import batched_values
from utils.s3_storage.keys import is_object_key_owned_by_tenant

#: Every model that stores a tenant-prefixed object key belongs here. Unlike the
#: abandoned-upload sweep there are no exemptions: this command only reads, so auditing
#: a model that turns out to be someone else's responsibility costs nothing, while
#: leaving one out means a cross-tenant key would never be found.
#:
#: The folder is the segment that follows the tenant prefix. Catalog media keys carry
#: the item id after it — ``t-<tenant>/catalog/<item>/<uuid>.jpg`` — so "catalog" is
#: the right bound to check: it is a prefix test, not an equality test.
#:
#: (model, folder, human label)
AUDITED = [
    (KBFiles, "kb_files", "Knowledge Base file"),
    (AgentCallRecording, "call_recordings", "Call recording"),
    (ContactImportFile, "contact_imports", "Contact import file"),
    (CatalogItemMedia, "catalog", "Catalog media"),
]


class Command(BaseCommand):
    help = (
        "Audits stored object keys for cross-tenant ownership mismatches left by the "
        "pre-fix confirm-upload flow. Read-only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="List every mismatched record instead of just the counts.",
        )
        parser.add_argument(
            "--tenant-id",
            type=str,
            default=None,
            help="Restrict the audit to one tenant.",
        )

    def handle(self, *args, **options):
        verbose = options["verbose"]
        tenant_id = options["tenant_id"]

        total_checked = 0
        total_bad = 0

        for model, folder, label in AUDITED:
            queryset = model.all_objects.all()
            if tenant_id:
                queryset = queryset.filter(tenant_id=tenant_id)

            checked = 0
            mismatches = []

            # values_list keeps this light, and keyset batching avoids the server-side
            # cursor that a pooler in transaction mode rejects.
            for record_id, tenant, key, _pk in batched_values(
                queryset, ["id", "tenant_id", "file"]
            ):
                if not key:
                    continue

                checked += 1
                if not is_object_key_owned_by_tenant(key, tenant, folder=folder):
                    mismatches.append((record_id, tenant, key))

            total_checked += checked
            total_bad += len(mismatches)

            if mismatches:
                self.stdout.write(
                    self.style.ERROR(
                        f"{label}: {len(mismatches)} of {checked} record(s) reference a "
                        f"key outside their own tenant."
                    )
                )
                shown = mismatches if verbose else mismatches[:10]
                for record_id, tenant, key in shown:
                    self.stdout.write(f"    id={record_id}  tenant={tenant}  key={key}")
                if not verbose and len(mismatches) > len(shown):
                    self.stdout.write(
                        f"    ... and {len(mismatches) - len(shown)} more (use --verbose)"
                    )
            else:
                self.stdout.write(
                    self.style.SUCCESS(f"{label}: {checked} record(s) checked, all clean.")
                )

        self.stdout.write("")
        if total_bad:
            self.stdout.write(
                self.style.ERROR(
                    f"{total_bad} of {total_checked} record(s) reference another tenant's "
                    "object.\n"
                    "This is the blast radius of the pre-fix confirm-upload hole. Each row "
                    "means one tenant could read another tenant's file. Treat as a security "
                    "incident: preserve this output, identify the affected tenants, and "
                    "follow your disclosure process before deleting anything."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"No cross-tenant object keys found across {total_checked} record(s). "
                    "No evidence the hole was exploited."
                )
            )

        return None
