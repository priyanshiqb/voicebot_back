"""
Upload tickets: what the client is handed, and what the ticket can and cannot promise.

A SAS PUT cannot bound an upload. Azure has no signable size condition at any API
version, so a ticket issued for a 1 MB file will take 500 MB and the overage is only
found at confirm, after it has been transferred and stored. That is not a gap to be
closed later — the S3 presigned POST that *could* sign a ``content-length-range`` has no
Azure equivalent, and the option is gone rather than unimplemented.

Two things follow, and both are asserted here because both are easy to lose in a
refactor:

- The confirm-time measure-and-delete is the only size enforcement there is, so each
  flow still has to sign *its own* ceiling into the ticket it returns.
- The ticket has to carry ``upload_headers``. Azure rejects a block-blob PUT without
  ``x-ms-blob-type``, and that rejection comes from the storage account rather than from
  this API — a client that drops the headers sees what looks like a storage outage.
"""

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from django.core.checks import Error, Warning
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from core.checks import (
    check_object_storage_configured,
    check_upload_presign_method,
    check_upload_url_ttl,
)
from core.services import uploads as upload_service

#: Azurite's published development account. Public, documented by Microsoft, and valid
#: base64 — which is all `generate_blob_sas` needs to produce a real signature offline.
TEST_ACCOUNT = "devstoreaccount1"
TEST_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/"
    "KBHBeksoGMGw=="
)


def fake_storage():
    storage = MagicMock()
    storage.get_presigned_put_url.return_value = (
        "https://acct.blob.core.windows.net/c/t-1/catalog/x.jpg?sig=…"
    )
    storage.get_upload_headers.return_value = {
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": "image/jpeg",
    }
    return storage


class UploadTicketTests(SimpleTestCase):
    def test_the_ticket_carries_a_put_url_and_no_form_fields(self):
        storage = fake_storage()

        ticket = upload_service.build_upload_ticket(
            "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024, storage=storage
        )

        self.assertEqual(ticket["upload_method"], "PUT")
        self.assertEqual(
            ticket["upload_url"],
            "https://acct.blob.core.windows.net/c/t-1/catalog/x.jpg?sig=…",
        )
        self.assertEqual(ticket["max_bytes"], 1024)

    def test_upload_fields_stays_present_and_empty(self):
        """Kept in the envelope rather than removed. It held the S3 POST policy's signed
        form fields; a client that reads it unconditionally should get an empty dict, not
        a KeyError."""
        ticket = upload_service.build_upload_ticket(
            "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024, storage=fake_storage()
        )

        self.assertIn("upload_fields", ticket)
        self.assertEqual(ticket["upload_fields"], {})

    def test_the_ticket_carries_the_mandatory_blob_type_header(self):
        """Without this the browser's PUT is rejected by Azure, not by us, so the failure
        reads as a storage outage rather than as a missing header."""
        ticket = upload_service.build_upload_ticket(
            "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024, storage=fake_storage()
        )

        self.assertEqual(ticket["upload_headers"]["x-ms-blob-type"], "BlockBlob")

    def test_the_declared_content_type_reaches_the_headers(self):
        """A SAS cannot pin Content-Type on a write, so the only way the blob gets stored
        with the right type is for the client to send it."""
        storage = fake_storage()

        upload_service.build_upload_ticket(
            "t-1/kb_files/x.pdf", "application/pdf", max_bytes=1024, storage=storage
        )

        storage.get_upload_headers.assert_called_once_with("application/pdf")

    def test_the_method_is_case_and_space_insensitive(self):
        with self.settings(UPLOAD_PRESIGN_METHOD=" put "):
            ticket = upload_service.build_upload_ticket(
                "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024, storage=fake_storage()
            )

        self.assertEqual(ticket["upload_method"], "PUT")

    def test_an_unknown_method_raises_rather_than_guessing(self):
        """Falling back would silently downgrade a security control. Loud is correct —
        and core.checks catches it at deploy so this never happens on a request."""
        with self.settings(UPLOAD_PRESIGN_METHOD="MULTIPART"):
            with self.assertRaises(ImproperlyConfigured):
                upload_service.build_upload_ticket(
                    "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024,
                    storage=fake_storage(),
                )

    def test_post_is_rejected_rather_than_silently_downgraded(self):
        """A leftover UPLOAD_PRESIGN_METHOD=POST from the S3 configuration asked for a
        store-enforced size ceiling. Azure cannot provide one, and quietly issuing an
        unbounded PUT instead would answer a security request with a security hole."""
        with self.settings(UPLOAD_PRESIGN_METHOD="POST"):
            with self.assertRaises(ImproperlyConfigured):
                upload_service.build_upload_ticket(
                    "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024,
                    storage=fake_storage(),
                )

    def test_the_ttl_comes_from_settings(self):
        storage = fake_storage()

        with self.settings(UPLOAD_URL_TTL_MINUTES=5):
            ticket = upload_service.build_upload_ticket(
                "t-1/catalog/x.jpg", "image/jpeg", max_bytes=1024, storage=storage
            )

        self.assertEqual(ticket["expires_in_minutes"], 5)
        self.assertEqual(
            storage.get_presigned_put_url.call_args.kwargs["expiry_minutes"], 5
        )

    def test_the_default_is_put(self):
        self.assertEqual(upload_service.DEFAULT_UPLOAD_METHOD, "PUT")

    def test_put_is_the_only_offered_method(self):
        """Asserted so that re-adding POST is a deliberate act. The schema advertises
        UPLOAD_METHODS as the choices for `upload_method`, so a stray entry here becomes
        a value clients are told to expect and the service cannot produce."""
        self.assertEqual(upload_service.UPLOAD_METHODS, ("PUT",))


class SasContractTests(SimpleTestCase):
    """What actually gets signed.

    No mocking: `generate_blob_sas` is pure local crypto, so these run the real signer
    against the emulator's public development key and read the result back off the query
    string — which is exactly what Azure will parse.
    """

    settings_kwargs = dict(
        AZURE_ACCOUNT_NAME=TEST_ACCOUNT,
        AZURE_ACCOUNT_KEY=TEST_KEY,
        AZURE_CONTAINER="test-container",
        AZURE_CONNECTION_STRING=None,
        AZURE_PUBLIC_ENDPOINT=None,
        AZURE_ENDPOINT_SUFFIX="core.windows.net",
    )

    def _service(self, **overrides):
        from utils.blob_storage.service import AzureBlobStorageService

        with self.settings(**{**self.settings_kwargs, **overrides}):
            return AzureBlobStorageService()

    def _query(self, url):
        return parse_qs(urlparse(url).query)

    def test_an_upload_sas_grants_create_and_not_write(self):
        """`create` refuses a Put Blob against a blob that already exists; `write` does
        not. That is the one bound this ticket has that the presigned PUT it replaced did
        not, and widening it to fix a permission error would silently remove it."""
        url = self._service().get_presigned_put_url(
            "t-1/catalog/x.jpg", "image/jpeg", 1024
        )

        self.assertEqual(self._query(url)["sp"], ["c"])

    def test_an_upload_sas_is_scoped_to_one_blob(self):
        url = self._service().get_presigned_put_url(
            "t-1/catalog/x.jpg", "image/jpeg", 1024
        )

        self.assertEqual(self._query(url)["sr"], ["b"])
        self.assertTrue(
            urlparse(url).path.endswith("/test-container/t-1/catalog/x.jpg"),
            urlparse(url).path,
        )

    def test_a_download_sas_grants_read_only(self):
        url = self._service().get_presigned_url_for_object("t-1/kb_files/x.pdf")

        self.assertEqual(self._query(url)["sp"], ["r"])

    def test_a_plain_download_sas_overrides_no_headers(self):
        """The ETL service reads these URLs as plain GETs; an attachment disposition it
        never asked for has no business on them."""
        url = self._service().get_presigned_url_for_object("t-1/kb_files/x.pdf")

        self.assertNotIn("rscd", self._query(url))

    def test_a_named_download_sas_is_read_only_and_names_the_file(self):
        """Without the signed Content-Disposition a browser saves a report under its blob
        key — tenant prefix, report id and all. A quote in the name would end the filename
        parameter early, so it is dropped."""
        url = self._service().get_presigned_download_url(
            "t-1/campaign-reports/r-1_campaign-q1.xlsx", 'campaign-"q1".xlsx'
        )

        query = self._query(url)
        self.assertEqual(query["sp"], ["r"])
        self.assertEqual(query["rscd"], ['attachment; filename="campaign-q1.xlsx"'])

    def test_every_sas_is_signed_and_expires(self):
        url = self._service().get_presigned_put_url(
            "t-1/catalog/x.jpg", "image/jpeg", 1024
        )

        query = self._query(url)
        self.assertIn("sig", query)
        self.assertIn("se", query)

    def test_the_upload_headers_name_the_block_blob_type(self):
        self.assertEqual(
            self._service().get_upload_headers("image/jpeg"),
            {"x-ms-blob-type": "BlockBlob", "Content-Type": "image/jpeg"},
        )

    def test_the_public_endpoint_rewrites_the_host_and_keeps_the_path(self):
        """In Docker the app addresses Azurite as `azurite:10000`, which a browser cannot
        resolve. The emulator also puts the account name in the path, so a rewrite that
        replaced more than the host would drop `/devstoreaccount1` and 404."""
        url = self._service(
            AZURE_CONNECTION_STRING=(
                f"DefaultEndpointsProtocol=http;AccountName={TEST_ACCOUNT};"
                f"AccountKey={TEST_KEY};"
                f"BlobEndpoint=http://azurite:10000/{TEST_ACCOUNT};"
            ),
            AZURE_PUBLIC_ENDPOINT="http://127.0.0.1:10000",
        ).get_presigned_put_url("t-1/catalog/x.jpg", "image/jpeg", 1024)

        parsed = urlparse(url)
        self.assertEqual(parsed.netloc, "127.0.0.1:10000")
        self.assertEqual(
            parsed.path, f"/{TEST_ACCOUNT}/test-container/t-1/catalog/x.jpg"
        )

    def test_the_signature_survives_the_host_rewrite(self):
        """The reason the rewrite is legitimate here and was a gamble under S3: a service
        SAS signs the canonicalized resource, not the Host header. Same blob, same
        permissions, different host — same signature."""
        kwargs = dict(
            AZURE_CONNECTION_STRING=(
                f"DefaultEndpointsProtocol=http;AccountName={TEST_ACCOUNT};"
                f"AccountKey={TEST_KEY};"
                f"BlobEndpoint=http://azurite:10000/{TEST_ACCOUNT};"
            ),
        )
        direct = self._service(**kwargs, AZURE_PUBLIC_ENDPOINT=None)
        rewritten = self._service(**kwargs, AZURE_PUBLIC_ENDPOINT="http://127.0.0.1:10000")

        signed = self._query(
            direct.get_presigned_url_for_object("t-1/kb_files/x.pdf")
        )["sig"]
        swapped = self._query(
            rewritten.get_presigned_url_for_object("t-1/kb_files/x.pdf")
        )["sig"]

        self.assertEqual(signed, swapped)

    def test_signing_without_an_account_key_fails_loudly(self):
        """The silent alternative is a URL with no `sig`, which Azure answers with 403 —
        at upload time, in the browser, with nothing in our logs."""
        with self.assertRaises(ValueError):
            self._service(AZURE_ACCOUNT_KEY=None).get_presigned_put_url(
                "t-1/catalog/x.jpg", "image/jpeg", 1024
            )

    def test_signing_without_a_container_fails_loudly(self):
        with self.assertRaises(ValueError):
            self._service(AZURE_CONTAINER=None).get_presigned_put_url(
                "t-1/catalog/x.jpg", "image/jpeg", 1024
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
        tickets without the mandatory headers after the others were fixed — which is
        exactly how catalog media ended up missing from the sweep and the key audit."""
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
                    f"upload_service.build_upload_ticket, so its ticket will not carry "
                    f"upload_headers and every upload through it will be rejected by "
                    f"Azure.",
                )
                self.assertIn("build_upload_ticket", source)


class CatalogTicketEndToEndTests(TestCase):
    """One real request through a real endpoint.

    The unit tests above prove the builder; this proves the wiring — that the view
    reaches it, passes its own ceiling, and returns the ticket to the client intact.
    """

    def setUp(self):
        from core.tests.catalog.views.base import CatalogAPITestCase

        # Borrow the catalog fixtures without subclassing into its storage patch decorator.
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

    @patch("core.views.catalog_item_media.AzureBlobStorageService")
    def test_the_endpoint_returns_a_usable_ticket(self, storage):
        from core.tests.catalog.views.base import MEDIA

        storage.return_value.get_presigned_put_url.return_value = "https://blob/put"
        storage.return_value.get_upload_headers.return_value = {
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": "image/jpeg",
        }

        with self.settings(CATALOG_MAX_MEDIA_BYTES=2048):
            response = self.api.post(
                f"{MEDIA}request-upload-url/", self._payload(), format="json"
            )

        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["upload_method"], "PUT")
        self.assertEqual(data["upload_url"], "https://blob/put")
        self.assertEqual(data["upload_fields"], {})
        self.assertEqual(data["upload_headers"]["x-ms-blob-type"], "BlockBlob")
        self.assertEqual(data["max_bytes"], 2048)
        self.assertIn("media_id", data)
        # Still no object key of our own making in the response body.
        self.assertNotIn("object_key", data)

        # And the ceiling reached the signer, not just the response body.
        self.assertEqual(
            storage.return_value.get_presigned_put_url.call_args.args[2], 2048
        )

    @patch("core.views.catalog_item_media.AzureBlobStorageService")
    def test_a_presign_failure_marks_the_row_failed(self, storage):
        from core.models.catalog_item_media import CatalogItemMedia
        from core.tests.catalog.views.base import MEDIA

        storage.return_value.get_presigned_put_url.side_effect = RuntimeError("down")

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

    def test_post_is_an_error_with_an_explanation(self):
        """POST was valid under S3. Someone carrying it forward deserves to be told why
        it is gone rather than just that it is wrong."""
        with self.settings(UPLOAD_PRESIGN_METHOD="POST"):
            results = check_upload_presign_method(None)

        self.assertEqual(results[0].id, "core.E001")
        self.assertIn("Azure Blob has no equivalent", results[0].hint)

    def test_put_is_clean(self):
        """PUT cannot enforce a size ceiling during the upload, and on Azure nothing can,
        so this is not a warning — a check that can never be resolved only teaches people
        to ignore `manage.py check`. The bound lives at confirm instead."""
        with self.settings(UPLOAD_PRESIGN_METHOD="PUT"):
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


class StorageConfiguredCheckTests(SimpleTestCase):
    """The failure this catches is silent: uploads land on the container's own disk and
    disappear on the next restart, discovered later as a file that 404s."""

    def test_unconfigured_storage_warns_outside_debug(self):
        with self.settings(DEBUG=False, USE_AZURE_STORAGE=False, AZURE_CONTAINER=None):
            results = check_object_storage_configured(None)

        self.assertTrue(any(isinstance(r, Warning) for r in results))
        self.assertEqual(results[0].id, "core.W002")
        self.assertIn("AZURE_CONTAINER", results[0].hint)

    def test_local_development_is_not_nagged(self):
        with self.settings(DEBUG=True, USE_AZURE_STORAGE=False):
            self.assertEqual(check_object_storage_configured(None), [])

    def test_configured_storage_is_clean(self):
        with self.settings(DEBUG=False, USE_AZURE_STORAGE=True):
            self.assertEqual(check_object_storage_configured(None), [])
