"""
Media API — the two-step upload.

The assertions that matter most are the security ones, because this flow is the
deliberate replacement for a pattern that leaked:

* **No object key is ever returned or accepted.** `request-upload-url` hands back a
  `media_id`; `confirm-upload` takes one. The stateless alternative — return a key,
  accept a key — is what let one tenant confirm *and delete* another tenant's
  Knowledge Base files.
* **The key is built server-side, inside the caller's own prefix**, from a sanitised
  filename. A crafted `../../t-victim/x.jpg` must not steer it.
* **Another tenant's `media_id` is 404**, not 403 — a 403 confirms the row exists.

S3 is mocked at `core.views.catalog_item_media.S3StorageService`, matching how the
Knowledge Base upload tests do it.
"""

from unittest.mock import patch

from core.models.catalog_item_media import CatalogItemMedia
from core.tests.catalog.views.base import MEDIA, CatalogAPITestCase

UploadStatus = CatalogItemMedia.UploadStatus
MediaType = CatalogItemMedia.MediaType


class MediaAPITestCase(CatalogAPITestCase):
    def setUp(self):
        super().setUp()
        self.item = self.make_item("Nike Air Max")

    def upload_payload(self, **overrides):
        data = {
            "item": str(self.item.pk),
            "media_type": MediaType.IMAGE,
            "filename": "photo.jpg",
            "content_type": "image/jpeg",
            "file_size_in_bytes": 1024,
        }
        data.update(overrides)
        return data

    def request_upload(self, client=None, **overrides):
        client = client or self.api
        return client.post(
            f"{MEDIA}request-upload-url/", self.upload_payload(**overrides), format="json"
        )

    def make_media(self, **overrides):
        fields = {
            "tenant": self.tenant,
            "item": self.item,
            "original_name": "photo.jpg",
            "extension": ".jpg",
            "file_size_in_bytes": 1024,
            "upload_status": UploadStatus.CONFIRMED,
        }
        fields.update(overrides)
        return CatalogItemMedia.objects.create(**fields)


class AuthTests(MediaAPITestCase):
    def test_anonymous_is_refused(self):
        self.assertEqual(self.api_anon.get(MEDIA).status_code, 401)

    def test_a_machine_token_is_refused(self):
        self.assertEqual(self.api_machine.get(MEDIA).status_code, 403)
        self.assertEqual(self.request_upload(client=self.api_machine).status_code, 403)


class PlainCreateTests(MediaAPITestCase):
    def test_post_to_the_collection_is_405(self):
        """A media row without a file is not worth creating, and the only way to get a
        file here is the two-step flow."""
        response = self.api.post(MEDIA, {"item": str(self.item.pk)}, format="json")
        self.assert_error(response, 405, contains="request-upload-url")


@patch("core.views.catalog_item_media.S3StorageService")
class RequestUploadUrlTests(MediaAPITestCase):
    def test_it_returns_a_media_id_and_url_but_no_object_key(self, storage):
        """The entire point of the stateful flow."""
        storage.return_value.get_presigned_put_url.return_value = "https://s3/put"

        data = self.assert_ok(self.request_upload())

        self.assertIn("media_id", data)
        self.assertEqual(data["upload_url"], "https://s3/put")
        self.assertEqual(data["expires_in_minutes"], 15)
        self.assertNotIn("object_key", data)
        self.assertNotIn("file", data)

    def test_it_creates_a_pending_row_in_the_callers_tenant(self, storage):
        storage.return_value.get_presigned_put_url.return_value = "https://s3/put"

        data = self.assert_ok(self.request_upload())
        media = CatalogItemMedia.objects.get(pk=data["media_id"])

        self.assertEqual(media.upload_status, UploadStatus.PENDING)
        self.assertEqual(media.tenant_id, self.tenant.id)
        self.assertEqual(media.item_id, self.item.pk)
        self.assertEqual(media.original_name, "photo.jpg")
        self.assertEqual(media.extension, ".jpg")

    def test_the_key_sits_under_the_callers_tenant_prefix(self, storage):
        storage.return_value.get_presigned_put_url.return_value = "https://s3/put"

        data = self.assert_ok(self.request_upload())
        media = CatalogItemMedia.objects.get(pk=data["media_id"])

        self.assertTrue(media.file.name.startswith(f"t-{self.tenant.id}/catalog/"))
        self.assertIn(str(self.item.pk), media.file.name)

    def test_a_traversal_filename_cannot_steer_the_key(self, storage):
        """`../../t-victim/x.jpg` must be reduced to a basename before it is
        interpolated. This is the injection half of the vulnerability the flow
        replaces."""
        storage.return_value.get_presigned_put_url.return_value = "https://s3/put"

        data = self.assert_ok(self.request_upload(filename="../../t-victim/evil.jpg"))
        media = CatalogItemMedia.objects.get(pk=data["media_id"])

        self.assertTrue(media.file.name.startswith(f"t-{self.tenant.id}/catalog/"))
        self.assertNotIn("..", media.file.name)
        self.assertNotIn("t-victim", media.file.name)

    def test_another_tenants_item_reads_as_not_found(self, storage):
        foreign_item = self.make_item("Theirs", tenant=self.other_tenant)
        response = self.request_upload(item=str(foreign_item.pk))

        self.assert_error(response, 400, field="item", contains="No such item")
        self.assertFalse(CatalogItemMedia.objects.filter(item=foreign_item).exists())

    def test_a_disallowed_extension_never_reaches_storage(self, storage):
        response = self.request_upload(filename="malware.exe")

        self.assert_error(response, 400, field="filename")
        storage.return_value.get_presigned_put_url.assert_not_called()
        self.assertEqual(CatalogItemMedia.objects.count(), 0)

    def test_an_oversize_file_never_reaches_storage(self, storage):
        with self.settings(CATALOG_MAX_MEDIA_BYTES=1024):
            response = self.request_upload(file_size_in_bytes=4096)

        self.assert_error(response, 400, field="file_size_in_bytes")
        storage.return_value.get_presigned_put_url.assert_not_called()

    def test_the_per_item_cap_is_enforced(self, storage):
        with self.settings(CATALOG_MAX_MEDIA_PER_ITEM=1):
            self.make_media()
            self.assert_error(self.request_upload(), 400, field="item")

    def test_a_storage_failure_marks_the_row_failed_and_returns_503(self, storage):
        storage.return_value.get_presigned_put_url.side_effect = RuntimeError("down")

        response = self.request_upload()

        self.assert_error(response, 503)
        media = CatalogItemMedia.objects.first()
        self.assertEqual(media.upload_status, UploadStatus.FAILED)


@patch("core.views.catalog_item_media.S3StorageService")
class ConfirmUploadTests(MediaAPITestCase):
    def _pending(self):
        return self.make_media(upload_status=UploadStatus.PENDING, file="t-x/y.jpg")

    def test_a_confirmed_upload_flips_status_and_records_the_real_size(self, storage):
        """The declared size at step 1 is a client claim; the stored size is the fact."""
        storage.return_value.verify_object_exists.return_value = True
        storage.return_value.get_file_disk_usage.return_value = 4096
        media = self._pending()

        data = self.assert_ok(
            self.api.post(
                f"{MEDIA}confirm-upload/", {"media_id": str(media.pk)}, format="json"
            )
        )

        self.assertEqual(data["upload_status"], UploadStatus.CONFIRMED)
        media.refresh_from_db()
        self.assertEqual(media.file_size_in_bytes, 4096)

    def test_a_missing_object_marks_the_row_failed(self, storage):
        storage.return_value.verify_object_exists.return_value = False
        media = self._pending()

        self.assert_error(
            self.api.post(
                f"{MEDIA}confirm-upload/", {"media_id": str(media.pk)}, format="json"
            ),
            400,
            contains="not found in storage",
        )

        media.refresh_from_db()
        self.assertEqual(media.upload_status, UploadStatus.FAILED)

    def test_confirming_twice_is_idempotent(self, storage):
        """A retried confirm after a dropped response must not fail."""
        storage.return_value.verify_object_exists.return_value = True
        storage.return_value.get_file_disk_usage.return_value = 4096
        media = self._pending()

        first = self.api.post(
            f"{MEDIA}confirm-upload/", {"media_id": str(media.pk)}, format="json"
        )
        second = self.api.post(
            f"{MEDIA}confirm-upload/", {"media_id": str(media.pk)}, format="json"
        )

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertIn("already confirmed", second.data["message"])

    def test_another_tenants_media_id_is_404_not_403(self, storage):
        """403 would confirm the row exists. It also must not reach storage at all —
        the destructive branches of an upload flow run after this point."""
        foreign_item = self.make_item("Theirs", tenant=self.other_tenant)
        foreign = CatalogItemMedia.objects.create(
            tenant=self.other_tenant,
            item=foreign_item,
            original_name="theirs.jpg",
            upload_status=UploadStatus.PENDING,
        )

        response = self.api.post(
            f"{MEDIA}confirm-upload/", {"media_id": str(foreign.pk)}, format="json"
        )

        self.assertEqual(response.status_code, 404, response.data)
        storage.return_value.verify_object_exists.assert_not_called()

        foreign.refresh_from_db()
        self.assertEqual(foreign.upload_status, UploadStatus.PENDING)

    def test_an_unknown_media_id_is_404(self, storage):
        response = self.api.post(
            f"{MEDIA}confirm-upload/",
            {"media_id": "00000000-0000-0000-0000-000000000000"},
            format="json",
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_malformed_media_id_is_a_400(self, storage):
        response = self.api.post(
            f"{MEDIA}confirm-upload/", {"media_id": "not-a-uuid"}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.data)

    def test_a_verification_failure_returns_503_and_leaves_the_row_pending(self, storage):
        storage.return_value.verify_object_exists.side_effect = RuntimeError("down")
        media = self._pending()

        self.assert_error(
            self.api.post(
                f"{MEDIA}confirm-upload/", {"media_id": str(media.pk)}, format="json"
            ),
            503,
        )

        media.refresh_from_db()
        self.assertEqual(media.upload_status, UploadStatus.PENDING)

    def test_confirming_as_primary_demotes_the_incumbent(self, storage):
        storage.return_value.verify_object_exists.return_value = True
        storage.return_value.get_file_disk_usage.return_value = 4096
        incumbent = self.make_media(is_primary=True, original_name="old.jpg")
        media = self._pending()

        self.assert_ok(
            self.api.post(
                f"{MEDIA}confirm-upload/",
                {"media_id": str(media.pk), "is_primary": True},
                format="json",
            )
        )

        incumbent.refresh_from_db()
        media.refresh_from_db()
        self.assertFalse(incumbent.is_primary)
        self.assertTrue(media.is_primary)


class UpdateTests(MediaAPITestCase):
    def test_only_presentation_fields_are_writable(self):
        media = self.make_media(upload_status=UploadStatus.PENDING)

        data = self.assert_ok(
            self.api.patch(
                f"{MEDIA}{media.pk}/",
                {
                    "alt_text": "  A shoe  ",
                    "sort_order": 3,
                    "upload_status": UploadStatus.CONFIRMED,
                    "file_size_in_bytes": 999,
                },
                format="json",
            )
        )

        self.assertEqual(data["alt_text"], "A shoe")
        self.assertEqual(data["sort_order"], 3)
        self.assertEqual(data["upload_status"], UploadStatus.PENDING)
        self.assertEqual(data["file_size_in_bytes"], 1024)

    def test_setting_primary_demotes_the_incumbent(self):
        incumbent = self.make_media(is_primary=True, original_name="old.jpg")
        challenger = self.make_media(original_name="new.jpg")

        self.assert_ok(
            self.api.patch(
                f"{MEDIA}{challenger.pk}/", {"is_primary": True}, format="json"
            )
        )

        incumbent.refresh_from_db()
        self.assertFalse(incumbent.is_primary)

    def test_delete_is_a_soft_delete(self):
        media = self.make_media()
        self.assertEqual(self.api.delete(f"{MEDIA}{media.pk}/").status_code, 200)
        self.assertTrue(CatalogItemMedia.all_objects.get(pk=media.pk).is_deleted)


class ListingTests(MediaAPITestCase):
    def test_filter_by_item_and_status(self):
        self.make_media(original_name="ok.jpg")
        self.make_media(original_name="pending.jpg", upload_status=UploadStatus.PENDING)

        self.assert_paginated(
            self.api.get(MEDIA, {"item": str(self.item.pk)}), count=2
        )
        rows = self.assert_paginated(
            self.api.get(MEDIA, {"upload_status": UploadStatus.PENDING}), count=1
        )
        self.assertEqual(rows[0]["original_name"], "pending.jpg")

    def test_search_matches_the_parent_item_name(self):
        self.make_media()
        self.assert_paginated(self.api.get(MEDIA, {"search": "Nike"}), count=1)

    def test_another_tenants_media_is_not_listed(self):
        self.make_media()
        foreign_item = self.make_item("Theirs", tenant=self.other_tenant)
        CatalogItemMedia.objects.create(
            tenant=self.other_tenant, item=foreign_item, original_name="theirs.jpg"
        )

        rows = self.assert_paginated(self.api.get(MEDIA), count=1)
        self.assertEqual(rows[0]["original_name"], "photo.jpg")

    def test_another_tenants_media_is_404_on_detail(self):
        foreign_item = self.make_item("Theirs", tenant=self.other_tenant)
        foreign = CatalogItemMedia.objects.create(
            tenant=self.other_tenant, item=foreign_item, original_name="theirs.jpg"
        )
        self.assertEqual(self.api.get(f"{MEDIA}{foreign.pk}/").status_code, 404)
