"""
The return shape of ``AzureBlobStorageService`` methods the confirm-upload flows depend on.

Every upload flow mocks ``AzureBlobStorageService`` — it has to, since the alternative is a
network call in a unit test. That makes the *shape* of what it returns a real contract,
and an unenforced one: a mock that returns something the original never would makes the
suite green while production takes a different branch.

That is not hypothetical. ``confirm-upload`` for catalog media read
``stats["size_in_bytes"]`` from a dict whose key is ``total_size_bytes``, so it always
fell back to the size the *client declared* and the size ceiling was never applied to
anything. The tests passed, because they stubbed the call with a bare integer that the
real method cannot return.

These tests call the real methods with only the Azure client replaced, so the keys are
pinned to the implementation rather than to a fixture. The stubs are built from the real
``BlobProperties`` class rather than from dicts, so a rename on the SDK side surfaces
here rather than in production.
"""

from unittest.mock import MagicMock, patch

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.storage.blob import BlobProperties
from django.test import SimpleTestCase

from utils.blob_storage.service import AzureBlobStorageService

#: Keys the confirm-upload views read off the result. Removing or renaming one is a
#: breaking change to every upload flow, not an internal refactor.
REQUIRED_DISK_USAGE_KEYS = {
    "total_size_bytes",
    "formatted_size",
    "content_type",
    "last_modified",
}


def blob_properties(size=None, content_type=None, last_modified=None, name=None):
    """A real ``BlobProperties``, keyed the way the SDK keys it.

    The wire names (``Content-Length``, not ``size``) are the SDK's own constructor
    contract; going through it rather than around it is what makes this a test of the
    integration instead of a test of a dict we wrote.
    """
    return BlobProperties(
        **{
            "name": name,
            "Content-Length": size,
            "Content-Type": content_type,
            "Last-Modified": last_modified,
        }
    )


def _service(blob_client=None, container_client=None):
    """Build the service with the Azure client replaced, without touching the network.

    Settings are left alone beyond the container and the public endpoint: presigning
    reads the account name and key straight from them, and those are real values.
    """
    service_client = MagicMock()
    if blob_client is not None:
        service_client.get_blob_client.return_value = blob_client
    if container_client is not None:
        service_client.get_container_client.return_value = container_client

    with patch("utils.blob_storage.service.BlobServiceClient") as client_cls:
        client_cls.return_value = service_client
        client_cls.from_connection_string.return_value = service_client
        service = AzureBlobStorageService()

    service.container_name = "test-container"
    service.public_endpoint = None
    return service


class GetFileDiskUsageContractTests(SimpleTestCase):
    def test_it_returns_a_dict_never_a_bare_size(self):
        """The bug in one assertion. A caller that treats the result as a number — or
        stubs it as one — is wrong, and this is what says so."""
        blob = MagicMock()
        blob.get_blob_properties.return_value = blob_properties(
            size=4096, content_type="image/jpeg"
        )

        result = _service(blob).get_file_disk_usage("t-1/catalog/x.jpg")

        self.assertIsInstance(result, dict)
        self.assertNotIsInstance(result, int)

    def test_the_size_key_is_total_size_bytes(self):
        blob = MagicMock()
        blob.get_blob_properties.return_value = blob_properties(size=4096)

        result = _service(blob).get_file_disk_usage("t-1/catalog/x.jpg")

        self.assertIn(
            "total_size_bytes",
            result,
            "the confirm-upload views read this exact key to enforce their size limits",
        )
        self.assertEqual(result["total_size_bytes"], 4096)
        self.assertNotIn(
            "size_in_bytes",
            result,
            "no such key exists — reading it silently yields the caller's default, "
            "which is how the catalog media ceiling went unenforced",
        )

    def test_every_documented_key_is_present(self):
        blob = MagicMock()
        blob.get_blob_properties.return_value = blob_properties(
            size=10, content_type="application/pdf"
        )

        result = _service(blob).get_file_disk_usage("t-1/kb_files/x.pdf")

        self.assertEqual(REQUIRED_DISK_USAGE_KEYS - set(result), set())

    def test_a_missing_object_reports_zero_rather_than_raising(self):
        """The views distinguish "gone" from "unreadable": a missing blob is a zero-size
        answer, anything else propagates and is handled as a verification failure."""
        blob = MagicMock()
        blob.get_blob_properties.side_effect = ResourceNotFoundError("gone")

        result = _service(blob).get_file_disk_usage("t-1/kb_files/gone.pdf")

        self.assertEqual(result["total_size_bytes"], 0)
        self.assertEqual(REQUIRED_DISK_USAGE_KEYS - set(result), set())

    def test_a_real_error_propagates(self):
        """Access denied is not "zero bytes". Swallowing it would confirm an upload
        whose size was never actually checked."""
        blob = MagicMock()
        blob.get_blob_properties.side_effect = HttpResponseError("forbidden")

        with self.assertRaises(HttpResponseError):
            _service(blob).get_file_disk_usage("t-1/kb_files/x.pdf")


class VerifyObjectExistsContractTests(SimpleTestCase):
    def test_it_returns_a_bool(self):
        blob = MagicMock()
        blob.exists.return_value = True

        self.assertIs(_service(blob).verify_object_exists("t-1/kb_files/x.pdf"), True)

    def test_a_missing_object_is_false_not_an_exception(self):
        blob = MagicMock()
        blob.exists.return_value = False

        self.assertIs(_service(blob).verify_object_exists("t-1/kb_files/x.pdf"), False)

    def test_a_real_error_propagates(self):
        """False means "not there". An outage must not be reported as absence, or the
        views mark a perfectly good upload FAILED."""
        blob = MagicMock()
        blob.exists.side_effect = HttpResponseError("boom")

        with self.assertRaises(HttpResponseError):
            _service(blob).verify_object_exists("t-1/kb_files/x.pdf")


class DeleteBlobContractTests(SimpleTestCase):
    """Azure's delete is not idempotent and S3's was. This is where that is absorbed."""

    def test_deleting_a_missing_blob_succeeds(self):
        """``delete_blob`` raises ``ResourceNotFoundError`` where ``delete_object``
        returned success. Every caller — the pending-upload sweep, both oversize
        rejection paths, the contact-import cleanup — deletes an object it does not know
        still exists, so letting that surface would turn a no-op into a 500."""
        blob = MagicMock()
        blob.delete_blob.side_effect = ResourceNotFoundError("already gone")

        self.assertIs(_service(blob).delete_blob_direct("t-1/kb_files/gone.pdf"), True)

    def test_a_real_error_still_propagates(self):
        """"Already deleted" is fine. "Cannot reach storage" is not — a swallowed
        failure here leaks bytes the sweep will never look at again."""
        blob = MagicMock()
        blob.delete_blob.side_effect = HttpResponseError("forbidden")

        with self.assertRaises(HttpResponseError):
            _service(blob).delete_blob_direct("t-1/kb_files/x.pdf")


class IterObjectsContractTests(SimpleTestCase):
    """The orphan audit reads these three keys off every entry."""

    def test_entries_carry_key_size_and_last_modified(self):
        container = MagicMock()
        container.list_blobs.return_value = iter(
            [blob_properties(name="t-1/kb_files/a.pdf", size=12, last_modified="then")]
        )

        objects = list(_service(container_client=container).iter_objects("t-1/"))

        self.assertEqual(
            objects,
            [{"key": "t-1/kb_files/a.pdf", "size": 12, "last_modified": "then"}],
        )
        container.list_blobs.assert_called_once_with(name_starts_with="t-1/")


class MockShapeTests(SimpleTestCase):
    """The catalog media tests build their stub from a helper. Keep it honest."""

    def test_the_catalog_test_helper_matches_the_real_shape(self):
        from core.tests.catalog.views.test_catalog_item_media import disk_usage

        blob = MagicMock()
        blob.get_blob_properties.return_value = blob_properties(
            size=4096, content_type="image/jpeg"
        )
        real = _service(blob).get_file_disk_usage("t-1/catalog/x.jpg")

        self.assertEqual(
            set(disk_usage(4096)),
            set(real),
            "the catalog upload tests stub get_file_disk_usage with a different set of "
            "keys than the service returns — the exact drift that hid the unenforced "
            "size ceiling",
        )
