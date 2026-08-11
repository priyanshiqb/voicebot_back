"""
The abandoned-upload sweep: rows AND the objects they left in the bucket.

Two defects motivated these tests, and both were invisible in the product:

  1. ``CatalogItemMedia`` was never in the swept list. The module was written after the
     sweep and nobody went back. Abandoned catalog uploads accumulated forever.
  2. ``AgentCallRecording`` and ``CatalogItemMedia`` had no ``delete()`` override, so
     even when a row *was* swept its object stayed in the bucket with nothing left
     pointing at it — the row was the only record of the key.

Neither produced an error. That is the point: nothing in the API surfaces a PENDING
row, so the only symptom is a storage bill. ``test_every_upload_model_is_swept`` is the
guard that makes the first one impossible to repeat.
"""

from unittest.mock import patch

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models.agent_call_recording import AgentCallRecording
from core.models.catalog_item import CatalogItem
from core.models.catalog_item_media import CatalogItemMedia
from core.models.knowledge_base import KBFiles
from core.services import uploads as upload_service
from tenant.models.Tenant import Tenant


class PendingUploadSweepTestCase(TestCase):
    """Shared fixtures: one PENDING row per swept model, all backdated past the TTL."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-sweep")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-sweep")

        self.item = CatalogItem.objects.create(
            tenant=self.tenant,
            item_kind=CatalogItem.ItemKind.PHYSICAL_GOOD,
            name="Chair",
        )

    # ---------- builders ----------

    def make_kb_file(self, tenant=None, status=None, age_hours=48):
        record = KBFiles(
            tenant=tenant or self.tenant,
            original_name="guide.pdf",
            extension=".pdf",
            upload_status=status or KBFiles.UploadStatus.PENDING,
            is_processed=False,
            processing_status="PENDING",
        )
        record.file.name = f"t-{record.tenant_id}/kb_files/{record.id}_guide.pdf"
        record.save()
        return self._backdate(KBFiles, record, age_hours)

    def make_recording(self, tenant=None, status=None, age_hours=48):
        record = AgentCallRecording(
            tenant=tenant or self.tenant,
            original_name="call.mp3",
            extension=".mp3",
            upload_status=status or AgentCallRecording.UploadStatus.PENDING,
        )
        record.file.name = f"t-{record.tenant_id}/call_recordings/{record.id}_call.mp3"
        record.save()
        return self._backdate(AgentCallRecording, record, age_hours)

    def make_media(self, tenant=None, status=None, age_hours=48, item=None):
        record = CatalogItemMedia(
            tenant=tenant or self.tenant,
            item=item or self.item,
            media_type=CatalogItemMedia.MediaType.IMAGE,
            original_name="chair.jpg",
            extension=".jpg",
            upload_status=status or CatalogItemMedia.UploadStatus.PENDING,
        )
        record.file.name = f"t-{record.tenant_id}/catalog/{record.item_id}/{record.id}.jpg"
        record.save()
        return self._backdate(CatalogItemMedia, record, age_hours)

    @staticmethod
    def _backdate(model, record, age_hours):
        """``created_at`` is auto_now_add, so the age has to be written afterwards."""
        stamp = timezone.now() - timezone.timedelta(hours=age_hours)
        model.all_objects.filter(pk=record.pk).update(created_at=stamp)
        record.refresh_from_db()
        return record

    @staticmethod
    def _deleted_count(results, model_name):
        return next(r["deleted"] for r in results if r["model"] == model_name)


class SweepScopeTests(PendingUploadSweepTestCase):
    """What gets swept, and — more importantly — what does not."""

    def test_abandoned_rows_are_deleted_for_every_flow(self):
        kb, recording, media = self.make_kb_file(), self.make_recording(), self.make_media()

        upload_service.sweep_pending_uploads()

        self.assertFalse(KBFiles.all_objects.filter(pk=kb.pk).exists())
        self.assertFalse(AgentCallRecording.all_objects.filter(pk=recording.pk).exists())
        self.assertFalse(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())

    def test_catalog_media_is_swept(self):
        """The original defect, named. Catalog media was added to the project after the
        sweep existed and was simply never added to it."""
        media = self.make_media()

        results = upload_service.sweep_pending_uploads()

        self.assertEqual(self._deleted_count(results, "CatalogItemMedia"), 1)
        self.assertFalse(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())

    def test_an_upload_still_in_flight_is_left_alone(self):
        """A presigned URL lives 15 minutes and a slow client may still be uploading.
        Sweeping by age is only safe if recent rows are untouchable."""
        fresh = [
            self.make_kb_file(age_hours=1),
            self.make_recording(age_hours=1),
            self.make_media(age_hours=1),
        ]

        upload_service.sweep_pending_uploads()

        for record in fresh:
            self.assertTrue(
                type(record).all_objects.filter(pk=record.pk).exists(),
                f"{type(record).__name__} was swept while still in flight",
            )

    def test_confirmed_uploads_are_never_swept(self):
        """No age makes a confirmed file collectable — that would be data loss."""
        confirmed = [
            self.make_kb_file(status=KBFiles.UploadStatus.CONFIRMED, age_hours=10_000),
            self.make_recording(
                status=AgentCallRecording.UploadStatus.CONFIRMED, age_hours=10_000
            ),
            self.make_media(
                status=CatalogItemMedia.UploadStatus.CONFIRMED, age_hours=10_000
            ),
        ]

        upload_service.sweep_pending_uploads()

        for record in confirmed:
            self.assertTrue(
                type(record).all_objects.filter(pk=record.pk).exists(),
                f"a CONFIRMED {type(record).__name__} was deleted",
            )

    def test_failed_uploads_are_left_alone(self):
        """FAILED is terminal with nothing behind it — confirm found no object, or
        presigning itself failed. Sweeping them would only hide the diagnosis."""
        failed = self.make_media(
            status=CatalogItemMedia.UploadStatus.FAILED, age_hours=10_000
        )

        upload_service.sweep_pending_uploads()

        self.assertTrue(CatalogItemMedia.all_objects.filter(pk=failed.pk).exists())

    def test_a_soft_deleted_pending_row_is_still_swept(self):
        """It still owns its bytes. Using the default manager here would hide it from
        the sweep permanently — the exact case ``all_objects`` is for."""
        media = self.make_media()
        media.soft_delete()

        upload_service.sweep_pending_uploads()

        self.assertFalse(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())

    def test_the_tenant_filter_restricts_the_sweep(self):
        mine = self.make_media()
        theirs = self.make_kb_file(tenant=self.other_tenant)

        upload_service.sweep_pending_uploads(tenant_id=self.tenant.id)

        self.assertFalse(CatalogItemMedia.all_objects.filter(pk=mine.pk).exists())
        self.assertTrue(KBFiles.all_objects.filter(pk=theirs.pk).exists())

    def test_the_cutoff_is_configurable(self):
        media = self.make_media(age_hours=5)

        upload_service.sweep_pending_uploads(older_than_hours=100)
        self.assertTrue(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())

        upload_service.sweep_pending_uploads(older_than_hours=1)
        self.assertFalse(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())

    def test_the_ttl_setting_is_the_default_cutoff(self):
        media = self.make_media(age_hours=5)

        with self.settings(PENDING_UPLOAD_TTL_HOURS=100):
            upload_service.sweep_pending_uploads()
            self.assertTrue(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())

        with self.settings(PENDING_UPLOAD_TTL_HOURS=1):
            upload_service.sweep_pending_uploads()
            self.assertFalse(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())

    def test_a_dry_run_reports_without_deleting(self):
        self.make_kb_file()
        media = self.make_media()

        results = upload_service.sweep_pending_uploads(dry_run=True)

        self.assertTrue(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())
        for result in results:
            self.assertEqual(result["deleted"], 0)
        matched = {r["model"]: r["matched"] for r in results}
        self.assertEqual(matched["CatalogItemMedia"], 1)
        self.assertEqual(matched["KBFiles"], 1)
        self.assertEqual(
            [r["sample_ids"] for r in results if r["model"] == "CatalogItemMedia"][0],
            [str(media.pk)],
        )


class StoredObjectRemovalTests(PendingUploadSweepTestCase):
    """The half that is easy to miss: deleting the row must delete the bytes.

    An abandoned upload usually *does* have an object behind it — the common failure is
    a PUT that succeeded followed by a confirm that never arrived. Dropping the row
    without the object orphans it permanently, because the row held the only copy of
    the key.
    """

    def test_the_sweep_deletes_the_stored_object_for_every_flow(self):
        kb, recording, media = self.make_kb_file(), self.make_recording(), self.make_media()
        expected = {kb.file.name, recording.file.name, media.file.name}

        with patch("django.core.files.storage.FileSystemStorage.delete") as mock_delete:
            upload_service.sweep_pending_uploads()

        deleted_keys = {call.args[0] for call in mock_delete.call_args_list}
        self.assertEqual(
            expected - deleted_keys,
            set(),
            "these object keys were left orphaned in the bucket",
        )

    def test_deleting_a_recording_removes_its_object(self):
        recording = self.make_recording()
        # Read the key up front: FieldFile.delete() clears `name` as it goes, so
        # asserting against it afterwards compares to None and passes for the wrong
        # reason — or, as here, fails confusingly.
        object_key = recording.file.name

        with patch("django.core.files.storage.FileSystemStorage.delete") as mock_delete:
            recording.delete()

        mock_delete.assert_called_once_with(object_key)

    def test_deleting_catalog_media_removes_its_object(self):
        media = self.make_media()
        object_key = media.file.name

        with patch("django.core.files.storage.FileSystemStorage.delete") as mock_delete:
            media.delete()

        mock_delete.assert_called_once_with(object_key)

    def test_soft_delete_keeps_the_object(self):
        """A soft delete is restorable, so the file has to survive it. Only a hard
        delete may touch the bucket."""
        media = self.make_media(status=CatalogItemMedia.UploadStatus.CONFIRMED)

        with patch("django.core.files.storage.FileSystemStorage.delete") as mock_delete:
            media.soft_delete()

        mock_delete.assert_not_called()

    def test_an_unreachable_bucket_still_lets_the_row_go(self):
        """Otherwise a storage outage makes cleanup impossible forever, and the rows
        pile up behind an error nobody is watching."""
        media = self.make_media()

        with patch(
            "django.core.files.storage.FileSystemStorage.delete",
            side_effect=OSError("bucket unreachable"),
        ):
            results = upload_service.sweep_pending_uploads()

        self.assertFalse(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())
        self.assertEqual(self._deleted_count(results, "CatalogItemMedia"), 1)

    def test_one_bad_row_does_not_abort_the_sweep(self):
        doomed = self.make_media()
        healthy = self.make_kb_file()

        real_delete = CatalogItemMedia.delete

        def explode(self, *args, **kwargs):
            raise RuntimeError("row is wedged")

        with patch.object(CatalogItemMedia, "delete", explode):
            results = upload_service.sweep_pending_uploads()

        self.assertTrue(CatalogItemMedia.all_objects.filter(pk=doomed.pk).exists())
        self.assertFalse(KBFiles.all_objects.filter(pk=healthy.pk).exists())
        self.assertEqual(
            next(r["failed"] for r in results if r["model"] == "CatalogItemMedia"), 1
        )
        self.assertIs(CatalogItemMedia.delete, real_delete)


class SweepCoverageTests(TestCase):
    """Guards against the sweep silently falling behind the codebase."""

    #: Models with a PENDING upload state that another sweep owns. Contact import files
    #: belong to a session whose own TTL purges them, and two sweeps racing on one row
    #: is worse than one sweep.
    EXEMPT = {"ContactImportFile"}

    def test_every_upload_model_is_swept(self):
        """The regression that started this: a new two-step upload flow that nobody adds
        to the sweep leaks bytes forever, with no error to notice.

        Anything with an ``UploadStatus.PENDING`` and a file field must be swept here or
        listed in ``EXEMPT`` with a reason.
        """
        swept = {model.__name__ for model, _ in upload_service.SWEPT_MODELS}

        candidates = set()
        for model in apps.get_models():
            upload_status = getattr(model, "UploadStatus", None)
            if upload_status is None or not hasattr(upload_status, "PENDING"):
                continue
            if not any(f.name == "file" for f in model._meta.get_fields()):
                continue
            candidates.add(model.__name__)

        missing = candidates - swept - self.EXEMPT
        self.assertEqual(
            missing,
            set(),
            f"{sorted(missing)} reserve PENDING upload rows but are not swept. Add them "
            f"to core.services.uploads.SWEPT_MODELS, or to EXEMPT here with the reason "
            f"another sweep owns them.",
        )

    def test_every_swept_model_deletes_its_stored_object(self):
        """Being in the list is only half of it — a model whose delete() leaves the blob
        turns the sweep into a row-only cleanup that quietly orphans bytes."""
        for model, label in upload_service.SWEPT_MODELS:
            with self.subTest(model=model.__name__):
                self.assertIsNot(
                    model.delete,
                    apps.get_model("core", model.__name__).__bases__[0].delete,
                    f"{model.__name__} does not override delete(), so sweeping it "
                    f"orphans its object in the bucket.",
                )


class KeyOwnershipAuditTests(PendingUploadSweepTestCase):
    """``audit_upload_key_ownership`` had the same omission as the sweep.

    It finds records whose stored key sits outside their own tenant's prefix — the
    blast-radius check for the old cross-tenant confirm-upload hole. Catalog media was
    added to the project after the command and was never added to its list, so a
    mismatched catalog key would have gone unreported by the very tool whose job is to
    report it.
    """

    def _audit(self, *args):
        from io import StringIO

        out = StringIO()
        call_command("audit_upload_key_ownership", *args, stdout=out)
        return out.getvalue()

    def test_catalog_media_is_audited(self):
        self.make_media(status=CatalogItemMedia.UploadStatus.CONFIRMED)

        output = self._audit()

        self.assertIn("Catalog media", output)
        self.assertIn("all clean", output)

    def test_a_cross_tenant_catalog_key_is_flagged(self):
        """The whole point of the command. A catalog media row whose key lives under
        another tenant's prefix must be reported, not passed over."""
        media = self.make_media(status=CatalogItemMedia.UploadStatus.CONFIRMED)
        CatalogItemMedia.all_objects.filter(pk=media.pk).update(
            file=f"t-{self.other_tenant.id}/catalog/{self.item.pk}/stolen.jpg"
        )

        output = self._audit("--verbose")

        self.assertIn("reference a key outside their own tenant", output)
        self.assertIn(str(media.pk), output)

    def test_the_configured_folder_matches_real_catalog_keys(self):
        """A wrong folder string would flag every healthy row as an incident. Catalog
        keys nest the item id under the folder, so this has to stay a prefix test."""
        from core.management.commands.audit_upload_key_ownership import AUDITED
        from utils.s3_storage.keys import is_object_key_owned_by_tenant

        folder = next(f for model, f, _ in AUDITED if model is CatalogItemMedia)
        media = self.make_media()

        self.assertTrue(
            is_object_key_owned_by_tenant(media.file.name, self.tenant.id, folder=folder),
            f"a legitimately-built catalog key does not match folder '{folder}'",
        )

    def test_every_upload_model_is_audited(self):
        """No exemptions here, unlike the sweep: the audit is read-only, so covering a
        model that belongs to someone else costs nothing, while missing one means a
        cross-tenant key is never found."""
        from core.management.commands.audit_upload_key_ownership import AUDITED

        audited = {model.__name__ for model, _, _ in AUDITED}

        candidates = set()
        for model in apps.get_models():
            upload_status = getattr(model, "UploadStatus", None)
            if upload_status is None or not hasattr(upload_status, "PENDING"):
                continue
            if not any(f.name == "file" for f in model._meta.get_fields()):
                continue
            candidates.add(model.__name__)

        missing = candidates - audited
        self.assertEqual(
            missing,
            set(),
            f"{sorted(missing)} store tenant-prefixed object keys but are not audited. "
            f"Add them to AUDITED in audit_upload_key_ownership.",
        )


class BeatScheduleTests(TestCase):
    """The command existed and was correct for months — it was simply never scheduled."""

    def test_the_sweep_is_scheduled(self):
        entry = settings.CELERY_BEAT_SCHEDULE.get("uploads-cleanup-pending")
        self.assertIsNotNone(
            entry,
            "the abandoned-upload sweep is not in CELERY_BEAT_SCHEDULE; a cleanup that "
            "has to be remembered is a cleanup that does not run",
        )
        self.assertEqual(entry["task"], "core.tasks.cleanup_pending_uploads")

    def test_every_scheduled_task_actually_exists(self):
        """A typo in a beat entry fails silently in the worker, not at startup."""
        import importlib

        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            with self.subTest(schedule=name):
                module_path, _, attribute = entry["task"].rpartition(".")
                module = importlib.import_module(module_path)
                self.assertTrue(
                    hasattr(module, attribute),
                    f"beat entry '{name}' points at {entry['task']}, which does not exist",
                )


class ManagementCommandTests(PendingUploadSweepTestCase):
    """The command and the beat task must stay the same code path."""

    def test_the_command_sweeps(self):
        media = self.make_media()

        call_command("cleanup_pending_uploads")

        self.assertFalse(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())

    def test_the_command_dry_run_deletes_nothing(self):
        media = self.make_media()

        call_command("cleanup_pending_uploads", "--dry-run")

        self.assertTrue(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())

    def test_the_command_honours_the_age_flag(self):
        media = self.make_media(age_hours=5)

        call_command("cleanup_pending_uploads", "--older-than-hours", "100")

        self.assertTrue(CatalogItemMedia.all_objects.filter(pk=media.pk).exists())

    def test_the_task_delegates_to_the_same_sweep(self):
        from core.tasks import cleanup_pending_uploads

        self.make_media()
        self.make_kb_file()

        deleted = cleanup_pending_uploads()

        self.assertEqual(deleted, 2)
        self.assertFalse(CatalogItemMedia.all_objects.exists())
