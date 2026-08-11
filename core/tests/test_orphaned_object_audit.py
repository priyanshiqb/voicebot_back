"""
Reconciling the bucket against the database.

``cleanup_pending_uploads`` walks rows and deletes their objects. Nothing walked
objects, so an object with **no row at all** — from a hard delete predating the
``delete()`` overrides, a legacy upload path, or a request that wrote to storage and
then failed before saving its row — was invisible to every tool we had.

The risk profile is the inverse of the sweep's, and the tests reflect that. The sweep
deletes from the presence of a row; this deletes from the *absence* of one, so a wrong
answer destroys a customer's file rather than leaking disk. Most of what follows is
about what must **not** be treated as an orphan.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models.agent_call_recording import AgentCallRecording
from core.models.catalog_item import CatalogItem
from core.models.catalog_item_media import CatalogItemMedia
from core.models.knowledge_base import KBFiles
from core.services import uploads as upload_service
from tenant.models.Tenant import Tenant


class OrphanedObjectTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-orphan")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-orphan")
        self.item = CatalogItem.objects.create(
            tenant=self.tenant,
            item_kind=CatalogItem.ItemKind.PHYSICAL_GOOD,
            name="Chair",
        )
        self.old = timezone.now() - timedelta(days=7)
        self.recent = timezone.now() - timedelta(minutes=2)

    def prefix(self, tenant=None):
        return f"t-{(tenant or self.tenant).id}/"

    def storage_with(self, *objects):
        """A storage stub whose listing is whatever the test says is in the bucket."""
        storage = MagicMock()
        storage.iter_objects.return_value = iter(objects)
        return storage

    def obj(self, key, size=1024, last_modified=None):
        return {
            "key": key,
            "size": size,
            "last_modified": self.recent if last_modified is None else last_modified,
        }

    def make_kb_file(self, tenant=None, deleted=False):
        tenant = tenant or self.tenant
        record = KBFiles(
            tenant=tenant,
            original_name="guide.pdf",
            extension=".pdf",
            upload_status=KBFiles.UploadStatus.CONFIRMED,
            is_processed=False,
            processing_status="PENDING",
        )
        record.file.name = f"t-{tenant.id}/kb_files/{record.id}_guide.pdf"
        record.save()
        if deleted:
            record.is_deleted = True
            record.deleted_at = timezone.now()
            record.save(update_fields=["is_deleted", "deleted_at"])
        return record


class KnownKeyTests(OrphanedObjectTestCase):
    def test_it_collects_keys_from_every_model(self):
        kb = self.make_kb_file()

        recording = AgentCallRecording(tenant=self.tenant, original_name="call.mp3")
        recording.file.name = f"{self.prefix()}call_recordings/{recording.id}_call.mp3"
        recording.save()

        media = CatalogItemMedia(tenant=self.tenant, item=self.item)
        media.file.name = f"{self.prefix()}catalog/{self.item.pk}/{media.id}.jpg"
        media.save()

        known = upload_service.known_object_keys(self.tenant.id)

        self.assertIn(kb.file.name, known)
        self.assertIn(recording.file.name, known)
        self.assertIn(media.file.name, known)

    def test_a_soft_deleted_row_still_owns_its_key(self):
        """The most dangerous mistake available here. A soft delete is restorable, so
        treating its file as unaccounted-for means --delete destroys the very thing a
        restore would need."""
        kb = self.make_kb_file(deleted=True)

        self.assertIn(kb.file.name, upload_service.known_object_keys(self.tenant.id))

    def test_keys_are_scoped_to_one_tenant(self):
        mine = self.make_kb_file()
        theirs = self.make_kb_file(tenant=self.other_tenant)

        known = upload_service.known_object_keys(self.tenant.id)

        self.assertIn(mine.file.name, known)
        self.assertNotIn(theirs.file.name, known)


class FindOrphanTests(OrphanedObjectTestCase):
    def test_an_object_with_no_row_is_an_orphan(self):
        storage = self.storage_with(
            self.obj(f"{self.prefix()}kb_files/ghost.pdf", last_modified=self.old)
        )

        orphans = upload_service.find_orphaned_objects(self.tenant.id, storage=storage)

        self.assertEqual([o["key"] for o in orphans], [f"{self.prefix()}kb_files/ghost.pdf"])

    def test_an_object_with_a_row_is_not_an_orphan(self):
        kb = self.make_kb_file()
        storage = self.storage_with(self.obj(kb.file.name, last_modified=self.old))

        self.assertEqual(
            upload_service.find_orphaned_objects(self.tenant.id, storage=storage), []
        )

    def test_a_recent_object_is_left_alone(self):
        """It may belong to a row that is about to be written. Age is the only thing
        separating "orphan" from "in flight"."""
        storage = self.storage_with(
            self.obj(f"{self.prefix()}kb_files/fresh.pdf", last_modified=self.recent)
        )

        self.assertEqual(
            upload_service.find_orphaned_objects(self.tenant.id, storage=storage), []
        )

    def test_an_object_without_a_timestamp_is_left_alone(self):
        """Cannot prove it is old enough, so it cannot be proven safe to delete."""
        storage = self.storage_with(
            self.obj(f"{self.prefix()}kb_files/undated.pdf", last_modified=None)
        )
        storage.iter_objects.return_value = iter(
            [{"key": f"{self.prefix()}kb_files/undated.pdf", "size": 1, "last_modified": None}]
        )

        self.assertEqual(
            upload_service.find_orphaned_objects(self.tenant.id, storage=storage), []
        )

    def test_it_only_lists_the_tenants_own_prefix(self):
        """A bug here would let one tenant's audit delete another's files."""
        storage = self.storage_with()

        upload_service.find_orphaned_objects(self.tenant.id, storage=storage)

        storage.iter_objects.assert_called_once_with(f"t-{self.tenant.id}/")

    def test_the_age_threshold_is_configurable(self):
        key = f"{self.prefix()}kb_files/aging.pdf"
        two_hours_ago = timezone.now() - timedelta(hours=2)

        storage = self.storage_with(self.obj(key, last_modified=two_hours_ago))
        self.assertEqual(
            upload_service.find_orphaned_objects(
                self.tenant.id, older_than_hours=1, storage=storage
            )[0]["key"],
            key,
        )

        storage = self.storage_with(self.obj(key, last_modified=two_hours_ago))
        self.assertEqual(
            upload_service.find_orphaned_objects(
                self.tenant.id, older_than_hours=24, storage=storage
            ),
            [],
        )

    def test_the_size_is_reported(self):
        storage = self.storage_with(
            self.obj(f"{self.prefix()}kb_files/big.pdf", size=5_000_000, last_modified=self.old)
        )

        orphans = upload_service.find_orphaned_objects(self.tenant.id, storage=storage)

        self.assertEqual(orphans[0]["size"], 5_000_000)


class CommandTests(OrphanedObjectTestCase):
    def _run(self, storage, *args):
        from unittest.mock import patch

        out = StringIO()
        with patch(
            "core.management.commands.audit_orphaned_objects.S3StorageService",
            return_value=storage,
        ):
            call_command("audit_orphaned_objects", *args, stdout=out)
        return out.getvalue()

    def test_it_reports_without_deleting_by_default(self):
        """Deleting from the ABSENCE of a row is destructive if the query is wrong, so
        the destructive path must be opt-in."""
        storage = self.storage_with(
            self.obj(f"{self.prefix()}kb_files/ghost.pdf", last_modified=self.old)
        )

        output = self._run(storage, "--tenant-id", str(self.tenant.id))

        self.assertIn("ghost.pdf", output)
        self.assertIn("Nothing was deleted", output)
        storage.delete_blob_direct.assert_not_called()

    def test_delete_removes_them(self):
        key = f"{self.prefix()}kb_files/ghost.pdf"
        storage = self.storage_with(self.obj(key, last_modified=self.old))

        output = self._run(storage, "--tenant-id", str(self.tenant.id), "--delete")

        storage.delete_blob_direct.assert_called_once_with(key)
        self.assertIn("Deleted 1", output)

    def test_a_clean_tenant_says_so(self):
        storage = self.storage_with()

        output = self._run(storage, "--tenant-id", str(self.tenant.id))

        self.assertIn("no orphaned objects", output.lower())

    def test_an_unknown_tenant_is_an_error(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self._run(
                self.storage_with(),
                "--tenant-id",
                "00000000-0000-0000-0000-000000000000",
            )

    def test_one_failing_tenant_does_not_stop_the_audit(self):
        storage = MagicMock()
        storage.iter_objects.side_effect = RuntimeError("bucket unreachable")

        output = self._run(storage)

        self.assertIn("could not be audited", output)
