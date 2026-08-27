"""
Does the file match what it claims to be?

Both the declared ``content_type`` and the filename's extension are client strings. The
allowlists check the string; nothing has ever looked at the bytes. A ``.png`` holding a
PDF passed every check in this codebase.

The rule under test is deliberately asymmetric, and most of these tests exist to hold
that shape rather than to prove any one signature:

* bytes that **positively identify** a contradicting type  → reject
* bytes we cannot identify, or an extension with no signature → allow

The second half is the important one. `.txt` and `.csv` have no magic number, so a
strict allowlist would reject every legitimate one. Failing to recognise a file is not
evidence against it.
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from core.services import uploads as upload_service
from utils.files import signatures

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"\x00" * 32
ZIP = b"PK\x03\x04" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32
MP4 = b"\x00\x00\x00\x20" + b"ftyp" + b"isom" + b"\x00" * 32
AVIF = b"\x00\x00\x00\x20" + b"ftyp" + b"avif" + b"\x00" * 32
MOV = b"\x00\x00\x00\x20" + b"ftyp" + b"qt  " + b"\x00" * 32
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 32
MP3_ID3 = b"ID3\x04\x00" + b"\x00" * 32
MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 32
EXE = b"MZ\x90\x00" + b"\x00" * 32
ELF = b"\x7fELF\x02\x01" + b"\x00" * 32
SCRIPT = b"#!/bin/sh\nrm -rf /\n"
HTML = b"<!DOCTYPE html><html><script>alert(1)</script>"
CSV = b"name,email,phone\nAda,ada@example.com,+91900000000\n"
TEXT = b"just some notes about the product\n"


class SniffTests(SimpleTestCase):
    def test_it_names_the_formats_the_upload_flows_accept(self):
        for payload, expected in (
            (PNG, "png"),
            (JPEG, "jpg"),
            (GIF, "gif"),
            (WEBP, "webp"),
            (AVIF, "avif"),
            (MP4, "mp4"),
            (MOV, "mov"),
            (WEBM, "webm"),
            (PDF, "pdf"),
            (ZIP, "zip"),
            (MP3_ID3, "mp3"),
            (MP3_FRAME, "mp3"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(signatures.sniff(payload), expected)

    def test_it_names_things_that_should_never_be_uploaded(self):
        self.assertEqual(signatures.sniff(EXE), "executable")
        self.assertEqual(signatures.sniff(ELF), "executable")
        self.assertEqual(signatures.sniff(SCRIPT), "script")

    def test_text_shaped_content_is_unidentifiable(self):
        """Not a gap — the basis of the allow-on-unknown rule. CSV and plain text have
        no signature, so the check must have nothing to say about them."""
        self.assertIsNone(signatures.sniff(CSV))
        self.assertIsNone(signatures.sniff(TEXT))
        self.assertIsNone(signatures.sniff(HTML))

    def test_empty_and_short_input_is_safe(self):
        for payload in (b"", b"\xff", b"PK"):
            with self.subTest(payload=payload):
                self.assertIsNone(signatures.sniff(payload))

    def test_a_riff_container_is_read_at_the_right_offset(self):
        """WEBP names its payload at byte 8. Reading only byte 0 sees `RIFF` and would
        call a WAV file a WebP."""
        wav = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 32
        self.assertEqual(signatures.sniff(WEBP), "webp")
        self.assertEqual(signatures.sniff(wav), "wav")


class ContradictionTests(SimpleTestCase):
    def test_a_pdf_wearing_a_png_extension_is_caught(self):
        mismatch, detected = signatures.contradicts_extension(PDF, ".png")
        self.assertTrue(mismatch)
        self.assertEqual(detected, "pdf")

    def test_an_executable_renamed_to_an_image_is_caught(self):
        mismatch, detected = signatures.contradicts_extension(EXE, ".jpg")
        self.assertTrue(mismatch)
        self.assertEqual(detected, "executable")

    def test_matching_content_passes(self):
        for payload, extension in (
            (PNG, ".png"),
            (JPEG, ".jpg"),
            (JPEG, ".jpeg"),
            (PDF, ".pdf"),
            (WEBP, ".webp"),
            (AVIF, ".avif"),
            (MP3_ID3, ".mp3"),
            (WEBM, ".webm"),
        ):
            with self.subTest(extension=extension):
                mismatch, _ = signatures.contradicts_extension(payload, extension)
                self.assertFalse(mismatch)

    def test_office_files_are_accepted_as_zip(self):
        """.docx and .xlsx *are* ZIP archives. Telling them apart means parsing untrusted
        archive structure to answer a question the extension already answers."""
        for extension in (".docx", ".xlsx"):
            with self.subTest(extension=extension):
                mismatch, detected = signatures.contradicts_extension(ZIP, extension)
                self.assertFalse(mismatch)
                self.assertEqual(detected, "zip")

    def test_mp4_and_mov_are_interchangeable(self):
        """Same ISO base media container; the brand is optional."""
        for payload in (MP4, MOV):
            for extension in (".mp4", ".mov"):
                with self.subTest(payload=payload[:12], extension=extension):
                    mismatch, _ = signatures.contradicts_extension(payload, extension)
                    self.assertFalse(mismatch)

    def test_unidentifiable_content_is_allowed(self):
        """The rule that keeps false rejections near zero."""
        mismatch, detected = signatures.contradicts_extension(CSV, ".csv")
        self.assertFalse(mismatch)
        self.assertIsNone(detected)

    def test_an_extension_without_a_signature_never_rejects(self):
        """Even when the bytes ARE identifiable. A .txt holding a PDF is odd, but
        rejecting it means guessing at intent, and .txt promises nothing."""
        mismatch, detected = signatures.contradicts_extension(PDF, ".txt")
        self.assertFalse(mismatch)
        self.assertEqual(detected, "pdf")

    def test_an_unknown_extension_never_rejects(self):
        mismatch, _ = signatures.contradicts_extension(PDF, ".xyz")
        self.assertFalse(mismatch)

    def test_the_extension_dot_is_optional_and_case_insensitive(self):
        for extension in (".PNG", "PNG", "png", " .Png "):
            with self.subTest(extension=extension):
                mismatch, _ = signatures.contradicts_extension(PDF, extension)
                self.assertTrue(mismatch, f"{extension!r} was not normalised")


class ContentMismatchServiceTests(SimpleTestCase):
    def _storage(self, head):
        storage = MagicMock()
        storage.get_object_head_bytes.return_value = head
        return storage

    def test_it_reads_only_the_head(self):
        """A ranged GET is what makes this affordable on a 50 MB recording."""
        storage = self._storage(PNG)

        upload_service.content_mismatch(storage, "t-1/catalog/x.png", ".png")

        kwargs = storage.get_object_head_bytes.call_args.kwargs
        self.assertEqual(kwargs["num_bytes"], signatures.HEAD_BYTES)
        self.assertLessEqual(signatures.HEAD_BYTES, 512)

    def test_a_mismatch_is_reported(self):
        mismatch, detected = upload_service.content_mismatch(
            self._storage(PDF), "t-1/catalog/x.png", ".png"
        )
        self.assertTrue(mismatch)
        self.assertEqual(detected, "pdf")

    def test_an_empty_read_is_not_a_mismatch(self):
        """A file too short to hold a signature, or a backend without ranged reads.
        Neither is evidence of anything."""
        mismatch, _ = upload_service.content_mismatch(
            self._storage(b""), "t-1/catalog/x.png", ".png"
        )
        self.assertFalse(mismatch)

    def test_the_check_can_be_switched_off(self):
        storage = self._storage(PDF)

        with self.settings(UPLOAD_VERIFY_CONTENT=False):
            mismatch, _ = upload_service.content_mismatch(
                storage, "t-1/catalog/x.png", ".png"
            )

        self.assertFalse(mismatch)
        storage.get_object_head_bytes.assert_not_called()

    def test_storage_errors_propagate(self):
        """The callers fail closed on this. Swallowing it here would make "break the
        ranged GET" the way around the check."""
        storage = MagicMock()
        storage.get_object_head_bytes.side_effect = RuntimeError("down")

        with self.assertRaises(RuntimeError):
            upload_service.content_mismatch(storage, "t-1/catalog/x.png", ".png")


class RangedReadContractTests(SimpleTestCase):
    """`get_object_head_bytes` must cost bytes, not megabytes, and must not raise on
    the ordinary "nothing there" cases."""

    def _service(self, blob):
        from utils.blob_storage.service import AzureBlobStorageService

        service_client = MagicMock()
        service_client.get_blob_client.return_value = blob
        with patch("utils.blob_storage.service.BlobServiceClient") as client_cls:
            client_cls.return_value = service_client
            client_cls.from_connection_string.return_value = service_client
            service = AzureBlobStorageService()
        service.container_name = "test-container"
        return service

    def _blob(self, **kwargs):
        blob = MagicMock()
        blob.download_blob.configure_mock(**kwargs)
        return blob

    def test_it_requests_a_byte_range(self):
        """The whole affordability argument: identifying a 50 MB recording costs 64
        bytes. A read that forgot the range would still pass every assertion about the
        bytes it returned."""
        blob = self._blob(return_value=MagicMock(readall=lambda: PNG))

        self._service(blob).get_object_head_bytes("t-1/catalog/x.png", num_bytes=64)

        self.assertEqual(
            blob.download_blob.call_args.kwargs, {"offset": 0, "length": 64}
        )

    def test_a_missing_object_reads_as_empty(self):
        from azure.core.exceptions import ResourceNotFoundError

        blob = self._blob(side_effect=ResourceNotFoundError("gone"))

        self.assertEqual(
            self._service(blob).get_object_head_bytes("t-1/catalog/gone.png"), b""
        )

    def test_a_range_the_backend_rejects_reads_as_empty(self):
        """An empty blob: Azure answers InvalidRange rather than returning nothing. Such
        a file cannot carry a signature, so empty is the right answer."""
        from azure.core.exceptions import HttpResponseError

        error = HttpResponseError("range")
        error.error_code = "InvalidRange"
        blob = self._blob(side_effect=error)

        self.assertEqual(
            self._service(blob).get_object_head_bytes("t-1/catalog/tiny.png"), b""
        )

    def test_a_short_object_returns_what_there_is(self):
        """Azure truncates a range that overruns the blob rather than failing it, so the
        short-file path is an ordinary read — the signature check then decides whether
        those few bytes identify anything."""
        blob = self._blob(return_value=MagicMock(readall=lambda: PNG[:8]))

        self.assertEqual(
            self._service(blob).get_object_head_bytes("t-1/catalog/x.png"), PNG[:8]
        )

    def test_a_real_error_propagates(self):
        from azure.core.exceptions import HttpResponseError

        error = HttpResponseError("denied")
        error.error_code = "AuthorizationFailure"
        blob = self._blob(side_effect=error)

        with self.assertRaises(HttpResponseError):
            self._service(blob).get_object_head_bytes("t-1/catalog/x.png")


class CatalogConfirmContentTests(TestCase):
    """End-to-end through the endpoint, since the unit tests above cannot show that the
    view actually calls this or that a rejected upload is purged."""

    def setUp(self):
        from core.tests.catalog.views.base import CatalogAPITestCase

        self.helper = CatalogAPITestCase(methodName="run")
        self.helper.setUp()
        self.item = self.helper.make_item("Nike Air Max")
        self.api = self.helper.api

    def _pending(self):
        from core.models.catalog_item_media import CatalogItemMedia

        media = CatalogItemMedia.objects.create(
            tenant=self.helper.tenant,
            item=self.item,
            original_name="photo.jpg",
            extension=".jpg",
            file_size_in_bytes=1024,
            upload_status=CatalogItemMedia.UploadStatus.PENDING,
        )
        media.file.name = f"t-{self.helper.tenant.id}/catalog/{self.item.pk}/x.jpg"
        media.save()
        return media

    def _confirm(self, media):
        from core.tests.catalog.views.base import MEDIA

        return self.api.post(
            f"{MEDIA}confirm-upload/", {"media_id": str(media.pk)}, format="json"
        )

    @staticmethod
    def _wire(storage, head):
        storage.return_value.verify_object_exists.return_value = True
        storage.return_value.get_file_disk_usage.return_value = {
            "total_size_bytes": 4096,
            "formatted_size": "4.00 KB",
            "content_type": "image/jpeg",
            "last_modified": None,
        }
        storage.return_value.get_object_head_bytes.return_value = head

    @patch("core.views.catalog_item_media.AzureBlobStorageService")
    def test_a_pdf_uploaded_as_a_jpg_is_rejected_and_purged(self, storage):
        from core.models.catalog_item_media import CatalogItemMedia

        self._wire(storage, PDF)
        media = self._pending()

        response = self._confirm(media)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("pdf", str(response.data["message"]))
        media.refresh_from_db()
        self.assertEqual(media.upload_status, CatalogItemMedia.UploadStatus.FAILED)
        storage.return_value.delete_blob_direct.assert_called_once_with(media.file.name)

    @patch("core.views.catalog_item_media.AzureBlobStorageService")
    def test_an_executable_renamed_to_jpg_is_rejected(self, storage):
        self._wire(storage, EXE)
        media = self._pending()

        response = self._confirm(media)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("executable", str(response.data["message"]))

    @patch("core.views.catalog_item_media.AzureBlobStorageService")
    def test_a_real_jpeg_is_confirmed(self, storage):
        from core.models.catalog_item_media import CatalogItemMedia

        self._wire(storage, JPEG)
        media = self._pending()

        response = self._confirm(media)

        self.assertEqual(response.status_code, 200, response.data)
        media.refresh_from_db()
        self.assertEqual(media.upload_status, CatalogItemMedia.UploadStatus.CONFIRMED)
        storage.return_value.delete_blob_direct.assert_not_called()

    @patch("core.views.catalog_item_media.AzureBlobStorageService")
    def test_an_unreadable_head_fails_closed(self, storage):
        from core.models.catalog_item_media import CatalogItemMedia

        self._wire(storage, JPEG)
        storage.return_value.get_object_head_bytes.side_effect = RuntimeError("down")
        media = self._pending()

        response = self._confirm(media)

        self.assertEqual(response.status_code, 503, response.data)
        media.refresh_from_db()
        self.assertEqual(media.upload_status, CatalogItemMedia.UploadStatus.PENDING)

    @patch("core.views.catalog_item_media.AzureBlobStorageService")
    def test_the_check_can_be_switched_off_per_deployment(self, storage):
        from core.models.catalog_item_media import CatalogItemMedia

        self._wire(storage, PDF)
        media = self._pending()

        with self.settings(UPLOAD_VERIFY_CONTENT=False):
            response = self._confirm(media)

        self.assertEqual(response.status_code, 200, response.data)
        media.refresh_from_db()
        self.assertEqual(media.upload_status, CatalogItemMedia.UploadStatus.CONFIRMED)


class VerificationCoverageTests(TestCase):
    """Every confirm endpoint must run the check, or one flow keeps accepting mislabelled
    files after the others were fixed — the same drift that left catalog media out of the
    sweep and the key audit."""

    def test_every_confirm_endpoint_verifies_content(self):
        import inspect

        from core.views import agent_call_log, catalog_item_media, contact_import
        from core.views import knowledge_base

        for module in (catalog_item_media, knowledge_base, contact_import, agent_call_log):
            with self.subTest(module=module.__name__):
                self.assertIn(
                    "content_mismatch",
                    inspect.getsource(module),
                    f"{module.__name__} confirms uploads without checking the file's "
                    f"bytes against its declared extension.",
                )
