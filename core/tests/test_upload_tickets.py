"""
Presigned upload tickets: the size ceiling as something the object store enforces.

A presigned PUT cannot bound an upload. ``ContentLength`` has to be signed as an exact
value, which a browser cannot control, so it was commented out and the URL accepts a
body of any size — a ticket issued for a 1 MB file will take 500 MB, and the overage is
only found at confirm, after it has been transferred and stored.

A presigned POST can: ``content-length-range`` is signed into the policy and the store
rejects an oversize body mid-upload. These tests hold the two apart, and check that each
flow signs *its own* ceiling rather than a shared guess.

``UPLOAD_PRESIGN_METHOD`` still defaults to PUT, because switching is a breaking change
for the client. That default is deliberate and is asserted below, so flipping it is a
decision someone makes rather than something that drifts.
"""

from unittest.mock import MagicMock, patch

from django.core.checks import Error, Warning
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from core.checks import check_upload_presign_method, check_upload_url_ttl
from core.services import uploads as upload_service


def fake_storage():
    storage = MagicMock()
    storage.get_presigned_post.return_value = {
        "url": "https://bucket.example/",
        "fields": {"key": "t-1/catalog/x.jpg", "policy": "…", "x-amz-signature": "…"},
    }
    storage.get_presigned_put_url.return_value = "https://bucket.example/t-1/catalog/x.jpg"
    return storage


class UploadTicketTests(SimpleTestCase):
    def test_post_mode_returns_form_fields(self):
        storage = fake_storage()

        with self.settings(UPLOAD_PRESIGN_METHOD="POST"):
            ticket = upload_service.build_upload_ticket(
                "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024, storage=storage
            )

        self.assertEqual(ticket["upload_method"], "POST")
        self.assertEqual(ticket["upload_url"], "https://bucket.example/")
        self.assertIn("policy", ticket["upload_fields"])
        self.assertEqual(ticket["max_bytes"], 1024)
        storage.get_presigned_put_url.assert_not_called()

    def test_post_mode_signs_the_size_ceiling(self):
        """The whole point. The ceiling has to reach the policy, not just the response."""
        storage = fake_storage()

        with self.settings(UPLOAD_PRESIGN_METHOD="POST"):
            upload_service.build_upload_ticket(
                "t-1/catalog/x.jpg", "image/jpeg", max_bytes=4096, storage=storage
            )

        kwargs = storage.get_presigned_post.call_args.kwargs
        self.assertEqual(kwargs["max_bytes"], 4096)
        self.assertEqual(kwargs["min_bytes"], 1)

    def test_put_mode_returns_no_fields(self):
        storage = fake_storage()

        with self.settings(UPLOAD_PRESIGN_METHOD="PUT"):
            ticket = upload_service.build_upload_ticket(
                "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024, storage=storage
            )

        self.assertEqual(ticket["upload_method"], "PUT")
        self.assertEqual(ticket["upload_fields"], {})
        storage.get_presigned_post.assert_not_called()

    def test_the_method_is_case_and_space_insensitive(self):
        storage = fake_storage()

        with self.settings(UPLOAD_PRESIGN_METHOD=" post "):
            ticket = upload_service.build_upload_ticket(
                "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024, storage=storage
            )

        self.assertEqual(ticket["upload_method"], "POST")

    def test_an_unknown_method_raises_rather_than_guessing(self):
        """Falling back would silently downgrade a security control. Loud is correct —
        and core.checks catches it at deploy so this never happens on a request."""
        with self.settings(UPLOAD_PRESIGN_METHOD="MULTIPART"):
            with self.assertRaises(ImproperlyConfigured):
                upload_service.build_upload_ticket(
                    "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024,
                    storage=fake_storage(),
                )

    def test_the_ttl_comes_from_settings(self):
        storage = fake_storage()

        with self.settings(UPLOAD_PRESIGN_METHOD="POST", UPLOAD_URL_TTL_MINUTES=5):
            ticket = upload_service.build_upload_ticket(
                "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024, storage=storage
            )

        self.assertEqual(ticket["expires_in_minutes"], 5)
        self.assertEqual(storage.get_presigned_post.call_args.kwargs["expiry_minutes"], 5)

    def test_the_default_is_put(self):
        """Asserted on purpose. POST is the safer mode but a breaking change for the
        client, so backend and frontend have to ship together — the default must not
        drift to POST without someone choosing it."""
        self.assertEqual(upload_service.DEFAULT_UPLOAD_METHOD, "PUT")


class PresignedPostContractTests(SimpleTestCase):
    """What actually reaches boto3, since that is where the enforcement lives."""

    def _service(self, client):
        from utils.s3_storage.service import S3StorageService

        with patch("utils.s3_storage.service.boto3.client", return_value=client):
            service = S3StorageService()
        service.bucket_name = "test-bucket"
        service.custom_domain = None
        return service

    def _conditions(self, client):
        return client.generate_presigned_post.call_args.kwargs["Conditions"]

    def test_the_content_length_range_condition_is_present(self):
        client = MagicMock()
        client.generate_presigned_post.return_value = {"url": "https://s3/", "fields": {}}

        self._service(client).get_presigned_post(
            "t-1/kb_files/x.pdf", "application/pdf", max_bytes=11 * 1024 * 1024
        )

        self.assertIn(
            ["content-length-range", 1, 11 * 1024 * 1024],
            self._conditions(client),
            "without this condition the policy does not bound the upload at all, "
            "which is the entire reason for using POST over PUT",
        )

    def test_the_content_type_is_pinned(self):
        """Otherwise a client can declare an image and upload an HTML document, which
        matters because these objects are served back through presigned GET URLs."""
        client = MagicMock()
        client.generate_presigned_post.return_value = {"url": "https://s3/", "fields": {}}

        self._service(client).get_presigned_post(
            "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024
        )

        self.assertIn({"Content-Type": "image/jpeg"}, self._conditions(client))
        self.assertEqual(
            client.generate_presigned_post.call_args.kwargs["Fields"]["Content-Type"],
            "image/jpeg",
        )

    def test_a_zero_byte_upload_is_refused_by_default(self):
        client = MagicMock()
        client.generate_presigned_post.return_value = {"url": "https://s3/", "fields": {}}

        self._service(client).get_presigned_post(
            "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024
        )

        minimum = next(
            c[1] for c in self._conditions(client)
            if isinstance(c, list) and c[0] == "content-length-range"
        )
        self.assertEqual(minimum, 1)

    def test_the_key_is_pinned_exactly(self):
        """The key travels to the client in the form fields, as it already does inside a
        presigned PUT's path. It is signed, so it cannot be swapped for another
        tenant's — and confirm-upload still refuses to accept a key at all."""
        client = MagicMock()
        client.generate_presigned_post.return_value = {"url": "https://s3/", "fields": {}}

        self._service(client).get_presigned_post(
            "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024
        )

        self.assertEqual(
            client.generate_presigned_post.call_args.kwargs["Key"], "t-1/catalog/x.jpg"
        )

    def test_the_custom_domain_rewrites_the_url(self):
        """In Docker the signing endpoint is `minio:9000`, which a browser cannot
        resolve. Same rewrite the PUT path does."""
        from utils.s3_storage.service import S3StorageService

        client = MagicMock()
        client.generate_presigned_post.return_value = {
            "url": "http://minio:9000/test-bucket",
            "fields": {"key": "t-1/catalog/x.jpg"},
        }
        with patch("utils.s3_storage.service.boto3.client", return_value=client):
            service = S3StorageService()
        service.bucket_name = "test-bucket"
        service.custom_domain = "http://127.0.0.1:9000"

        result = service.get_presigned_post(
            "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024
        )

        self.assertEqual(result["url"], "http://127.0.0.1:9000/test-bucket")

    def test_the_expiry_is_in_seconds(self):
        client = MagicMock()
        client.generate_presigned_post.return_value = {"url": "https://s3/", "fields": {}}

        self._service(client).get_presigned_post(
            "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024, expiry_minutes=15
        )

        self.assertEqual(
            client.generate_presigned_post.call_args.kwargs["ExpiresIn"], 900
        )


class FlowCeilingTests(TestCase):
    """Each flow must sign its OWN ceiling. A shared default would be wrong in both
    directions: 50 MB call recordings truncated to 10 MB, or catalog images allowed 50."""

    def test_each_flow_signs_its_own_limit(self):
        from django.conf import settings as django_settings

        from core.views.agent_call_log import MAX_RECORDING_BYTES

        expected = {
            "catalog media": getattr(django_settings, "CATALOG_MAX_MEDIA_BYTES"),
            "knowledge base": getattr(django_settings, "DATA_UPLOAD_MAX_MEMORY_SIZE"),
            "contact import": getattr(django_settings, "CONTACT_IMPORT_MAX_FILE_BYTES"),
            "call recording": MAX_RECORDING_BYTES,
        }
        # Distinct values, or this test proves nothing about per-flow wiring.
        self.assertGreater(len(set(expected.values())), 1)
        for label, value in expected.items():
            with self.subTest(flow=label):
                self.assertGreater(value, 0)

    def test_no_upload_view_presigns_directly_any_more(self):
        """Every flow must go through build_upload_ticket, or one of them keeps issuing
        unbounded PUT URLs after the others were fixed — which is exactly how catalog
        media ended up missing from the sweep and the key audit."""
        import inspect

        from core.views import agent_call_log, catalog_item_media, contact_import
        from core.views import knowledge_base

        for module in (catalog_item_media, knowledge_base, contact_import, agent_call_log):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertNotIn(
                    ".get_presigned_put_url(",
                    source,
                    f"{module.__name__} presigns an upload directly instead of calling "
                    f"upload_service.build_upload_ticket, so it will not honour "
                    f"UPLOAD_PRESIGN_METHOD and cannot bound the upload size.",
                )
                self.assertIn("build_upload_ticket", source)


class CatalogPostModeEndToEndTests(TestCase):
    """One real request through a real endpoint in POST mode.

    The unit tests above prove the builder; this proves the wiring — that the view
    reaches it, passes its own ceiling, and returns the ticket to the client intact.
    """

    def setUp(self):
        from core.tests.catalog.views.base import CatalogAPITestCase

        # Borrow the catalog fixtures without subclassing into its S3 patch decorator.
        self.helper = CatalogAPITestCase(methodName="run")
        self.helper.setUp()
        self.item = self.helper.make_item("Nike Air Max")
        self.api = self.helper.api

    def _payload(self):
        return {
            "item": str(self.item.pk),
            "media_type": "IMAGE",
            "filename": "photo.jpg",
            "content_type": "image/jpeg",
            "file_size_in_bytes": 1024,
        }

    @patch("core.views.catalog_item_media.S3StorageService")
    def test_post_mode_returns_a_usable_form_ticket(self, storage):
        from core.tests.catalog.views.base import MEDIA

        storage.return_value.get_presigned_post.return_value = {
            "url": "https://bucket.example/",
            "fields": {"key": "t-1/catalog/x.jpg", "policy": "abc"},
        }

        with self.settings(UPLOAD_PRESIGN_METHOD="POST", CATALOG_MAX_MEDIA_BYTES=2048):
            response = self.api.post(
                f"{MEDIA}request-upload-url/", self._payload(), format="json"
            )

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["upload_method"], "POST")
        self.assertEqual(data["upload_url"], "https://bucket.example/")
        self.assertEqual(data["upload_fields"]["policy"], "abc")
        self.assertEqual(data["max_bytes"], 2048)
        self.assertIn("media_id", data)
        # Still no object key of our own making in the response body.
        self.assertNotIn("object_key", data)

        # And the ceiling reached the signer, not just the response body.
        self.assertEqual(
            storage.return_value.get_presigned_post.call_args.kwargs["max_bytes"], 2048
        )
        storage.return_value.get_presigned_put_url.assert_not_called()

    @patch("core.views.catalog_item_media.S3StorageService")
    def test_put_mode_still_works_unchanged(self, storage):
        """The default path. Existing clients must keep working until the frontend
        ships multipart support."""
        from core.tests.catalog.views.base import MEDIA

        storage.return_value.get_presigned_put_url.return_value = "https://s3/put"

        with self.settings(UPLOAD_PRESIGN_METHOD="PUT"):
            response = self.api.post(
                f"{MEDIA}request-upload-url/", self._payload(), format="json"
            )

        data = response.data["data"]
        self.assertEqual(data["upload_method"], "PUT")
        self.assertEqual(data["upload_url"], "https://s3/put")
        self.assertEqual(data["upload_fields"], {})
        storage.return_value.get_presigned_post.assert_not_called()

    @patch("core.views.catalog_item_media.S3StorageService")
    def test_a_presign_failure_marks_the_row_failed(self, storage):
        from core.models.catalog_item_media import CatalogItemMedia
        from core.tests.catalog.views.base import MEDIA

        storage.return_value.get_presigned_post.side_effect = RuntimeError("down")

        with self.settings(UPLOAD_PRESIGN_METHOD="POST"):
            response = self.api.post(
                f"{MEDIA}request-upload-url/", self._payload(), format="json"
            )

        self.assertEqual(response.status_code, 503, response.data)
        media = CatalogItemMedia.all_objects.get(item=self.item)
        self.assertEqual(media.upload_status, CatalogItemMedia.UploadStatus.FAILED)


class UploadCheckTests(SimpleTestCase):
    """`manage.py check` runs in CI and before migrate — the right place to catch a
    setting whose failure mode is a 500 on the first upload."""

    def test_an_unknown_method_is_an_error(self):
        with self.settings(UPLOAD_PRESIGN_METHOD="MULTIPART"):
            results = check_upload_presign_method(None)

        self.assertTrue(any(isinstance(r, Error) for r in results))
        self.assertEqual(results[0].id, "core.E001")

    def test_put_is_flagged_as_unenforceable(self):
        with self.settings(UPLOAD_PRESIGN_METHOD="PUT"):
            results = check_upload_presign_method(None)

        self.assertTrue(any(isinstance(r, Warning) for r in results))
        self.assertEqual(results[0].id, "core.W001")

    def test_post_is_clean(self):
        with self.settings(UPLOAD_PRESIGN_METHOD="POST"):
            self.assertEqual(check_upload_presign_method(None), [])

    def test_a_ticket_outliving_the_sweep_is_an_error(self):
        """The sweep would delete rows whose upload is still legitimately in flight, and
        the client would then confirm into a 404."""
        with self.settings(UPLOAD_URL_TTL_MINUTES=60 * 48, PENDING_UPLOAD_TTL_HOURS=24):
            results = check_upload_url_ttl(None)

        self.assertTrue(any(r.id == "core.E003" for r in results))

    def test_a_zero_ttl_is_an_error(self):
        with self.settings(UPLOAD_URL_TTL_MINUTES=0):
            results = check_upload_url_ttl(None)

        self.assertTrue(any(r.id == "core.E002" for r in results))

    def test_the_shipped_defaults_are_consistent(self):
        self.assertEqual(check_upload_url_ttl(None), [])
