"""
The return shape of ``S3StorageService`` methods the confirm-upload flows depend on.

Every upload flow mocks ``S3StorageService`` — it has to, since the alternative is a
network call in a unit test. That makes the *shape* of what it returns a real contract,
and an unenforced one: a mock that returns something the original never would makes the
suite green while production takes a different branch.

That is not hypothetical. ``confirm-upload`` for catalog media read
``stats["size_in_bytes"]`` from a dict whose key is ``total_size_bytes``, so it always
fell back to the size the *client declared* and the size ceiling was never applied to
anything. The tests passed, because they stubbed the call with a bare integer that the
real method cannot return.

These tests call the real methods with only the boto3 client replaced, so the keys are
pinned to the implementation rather than to a fixture.
"""

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

from utils.s3_storage.service import S3StorageService

#: Keys the confirm-upload views read off the result. Removing or renaming one is a
#: breaking change to every upload flow, not an internal refactor.
REQUIRED_DISK_USAGE_KEYS = {
    "total_size_bytes",
    "formatted_size",
    "content_type",
    "last_modified",
}


def _service(client):
    """Build the service with boto3 replaced, without touching settings or the network."""
    with patch("utils.s3_storage.service.boto3.client", return_value=client):
        service = S3StorageService()
    service.bucket_name = "test-bucket"
    service.custom_domain = None
    return service


class GetFileDiskUsageContractTests(SimpleTestCase):
    def test_it_returns_a_dict_never_a_bare_size(self):
        """The bug in one assertion. A caller that treats the result as a number — or
        stubs it as one — is wrong, and this is what says so."""
        client = MagicMock()
        client.head_object.return_value = {
            "ContentLength": 4096,
            "ContentType": "image/jpeg",
            "LastModified": None,
        }

        result = _service(client).get_file_disk_usage("t-1/catalog/x.jpg")

        self.assertIsInstance(result, dict)
        self.assertNotIsInstance(result, int)

    def test_the_size_key_is_total_size_bytes(self):
        client = MagicMock()
        client.head_object.return_value = {"ContentLength": 4096}

        result = _service(client).get_file_disk_usage("t-1/catalog/x.jpg")

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
        client = MagicMock()
        client.head_object.return_value = {
            "ContentLength": 10,
            "ContentType": "application/pdf",
            "LastModified": None,
        }

        result = _service(client).get_file_disk_usage("t-1/kb_files/x.pdf")

        self.assertEqual(REQUIRED_DISK_USAGE_KEYS - set(result), set())

    def test_a_missing_object_reports_zero_rather_than_raising(self):
        """The views distinguish "gone" from "unreadable": a 404 is a zero-size answer,
        anything else propagates and is handled as a verification failure."""
        client = MagicMock()
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )

        result = _service(client).get_file_disk_usage("t-1/kb_files/gone.pdf")

        self.assertEqual(result["total_size_bytes"], 0)
        self.assertEqual(REQUIRED_DISK_USAGE_KEYS - set(result), set())

    def test_a_real_error_propagates(self):
        """Access denied is not "zero bytes". Swallowing it would confirm an upload
        whose size was never actually checked."""
        client = MagicMock()
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "403"}}, "HeadObject"
        )

        with self.assertRaises(ClientError):
            _service(client).get_file_disk_usage("t-1/kb_files/x.pdf")


class VerifyObjectExistsContractTests(SimpleTestCase):
    def test_it_returns_a_bool(self):
        client = MagicMock()
        client.head_object.return_value = {"ContentLength": 1}

        self.assertIs(_service(client).verify_object_exists("t-1/kb_files/x.pdf"), True)

    def test_a_missing_object_is_false_not_an_exception(self):
        client = MagicMock()
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "HeadObject"
        )

        self.assertIs(_service(client).verify_object_exists("t-1/kb_files/x.pdf"), False)

    def test_a_real_error_propagates(self):
        """False means "not there". An outage must not be reported as absence, or the
        views mark a perfectly good upload FAILED."""
        client = MagicMock()
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "500"}}, "HeadObject"
        )

        with self.assertRaises(ClientError):
            _service(client).verify_object_exists("t-1/kb_files/x.pdf")


class MockShapeTests(SimpleTestCase):
    """The catalog media tests build their stub from a helper. Keep it honest."""

    def test_the_catalog_test_helper_matches_the_real_shape(self):
        from core.tests.catalog.views.test_catalog_item_media import disk_usage

        client = MagicMock()
        client.head_object.return_value = {
            "ContentLength": 4096,
            "ContentType": "image/jpeg",
            "LastModified": None,
        }
        real = _service(client).get_file_disk_usage("t-1/catalog/x.jpg")

        self.assertEqual(
            set(disk_usage(4096)),
            set(real),
            "the catalog upload tests stub get_file_disk_usage with a different set of "
            "keys than the service returns — the exact drift that hid the unenforced "
            "size ceiling",
        )
