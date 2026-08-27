"""
Azure Blob Storage client, and the one place provider differences are absorbed.

Replaces the boto3/S3 service. Every public method keeps its name, its arguments and
its return shape, so the confirm-upload flows read the same keys out of the same dicts
they always did — ``core/tests/test_storage_service_contract.py`` exists to hold that
line, because those dicts are mocked everywhere else and a renamed key would pass
every other test in the suite.

Three Azure facts shape this module. They are stated here rather than rediscovered:

- **A SAS cannot carry a size condition.** S3's presigned POST signs a
  ``content-length-range`` into the policy, so the store itself refuses an oversize
  body mid-upload. Azure has no equivalent at any API version, which is why
  ``get_presigned_post`` is gone rather than ported and why upload tickets are
  PUT-only. The size bound is entirely confirm-time: ``get_file_disk_usage`` then
  ``delete_blob_direct``.
- **A SAS cannot pin Content-Type on a write either.** The ``rsct`` field overrides
  the header on *reads*; there is no signed constraint on what a client uploads. The
  real defence against a mislabelled upload was never the header — it is the magic-byte
  check at confirm (``get_object_head_bytes`` into ``utils.files.signatures``), which
  reads the bytes themselves and does not care what anyone declared.
- **Delete is not idempotent.** ``delete_blob`` raises where S3's ``delete_object``
  returns success. The sweep and both oversize-rejection paths delete blobs that may
  already be gone, so a miss is swallowed here to keep the old contract.

Presigning is a pure local computation — account key in, signed URL out, no network
call and no client involved — so the URL is built from settings rather than read off a
``BlobClient``. That keeps ticket issuance offline and testable without a live account.
"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    generate_blob_sas,
)
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT_SUFFIX = "core.windows.net"

#: Mandatory on every SAS PUT. Azure answers ``400 An HTTP header that's mandatory for
#: this request is not specified`` without it, and the request is made by the browser
#: rather than by us, so the only way to get it there is to declare it in the ticket.
BLOB_TYPE_HEADER = "x-ms-blob-type"
BLOCK_BLOB = "BlockBlob"


class AzureBlobStorageService:
    """Service client helper for Azure Blob Storage (and Azurite, the local emulator)."""

    def __init__(self):
        self.account_name = getattr(settings, "AZURE_ACCOUNT_NAME", None)
        self.account_key = getattr(settings, "AZURE_ACCOUNT_KEY", None)
        self.container_name = getattr(settings, "AZURE_CONTAINER", None)
        self.connection_string = getattr(settings, "AZURE_CONNECTION_STRING", None)
        self.public_endpoint = getattr(settings, "AZURE_PUBLIC_ENDPOINT", None)

        if not self.account_name or not self.account_key or not self.container_name:
            logger.warning(
                "AzureBlobStorageService initialized without fully configuring "
                "AZURE_CONTAINER or credentials."
            )

        self.account_url = self._resolve_account_url()

        if self.connection_string:
            self.client = BlobServiceClient.from_connection_string(
                self.connection_string
            )
        else:
            self.client = BlobServiceClient(
                account_url=self.account_url,
                credential=self.account_key,
            )

    # --- addressing ----------------------------------------------------------

    def _resolve_account_url(self):
        """The account root: ``https://<acct>.blob.core.windows.net`` in production,
        ``http://azurite:10000/devstoreaccount1`` against the emulator.

        Those two shapes are why this is resolved once rather than assumed: Azurite puts
        the account in the *path* and production puts it in the *subdomain*, so anything
        appending ``/<container>/<blob>`` has to start from the right root.
        """
        if self.connection_string:
            parts = dict(
                segment.split("=", 1)
                for segment in self.connection_string.split(";")
                if "=" in segment
            )
            endpoint = parts.get("BlobEndpoint")
            if endpoint:
                return endpoint.rstrip("/")

            account = parts.get("AccountName") or self.account_name
            if account:
                protocol = parts.get("DefaultEndpointsProtocol", "https")
                suffix = parts.get("EndpointSuffix", DEFAULT_ENDPOINT_SUFFIX)
                return f"{protocol}://{account}.blob.{suffix}"

        suffix = getattr(settings, "AZURE_ENDPOINT_SUFFIX", DEFAULT_ENDPOINT_SUFFIX)
        return f"https://{self.account_name}.blob.{suffix}"

    def _blob_url(self, object_key):
        return f"{self.account_url}/{self.container_name}/{quote(object_key, safe='/')}"

    def _with_public_endpoint(self, url):
        """Swap in the browser-reachable host, when it differs from the internal one.

        This rewrite was a gamble under S3: SigV4 signs the ``Host`` header, so changing
        it after signing could invalidate the signature. An Azure service SAS signs the
        canonicalized resource ``/blob/<account>/<container>/<blob>`` and **not** the
        host, so the swap is legitimate. Azurite needs it — a browser on the host cannot
        resolve the ``azurite`` compose hostname.

        Only scheme and netloc move. The path is preserved, which matters for the
        emulator: ``/devstoreaccount1`` is part of the path, not the host.
        """
        if not self.public_endpoint:
            return url

        raw = self.public_endpoint
        custom = urlparse(raw if "://" in raw else f"http://{raw}")
        if not custom.netloc:
            return url

        parsed = urlparse(url)
        return url.replace(
            f"{parsed.scheme}://{parsed.netloc}",
            f"{custom.scheme}://{custom.netloc}",
            1,
        )

    def _blob(self, object_key):
        return self.client.get_blob_client(
            container=self.container_name, blob=object_key
        )

    def _require_container(self):
        if not self.container_name:
            raise ValueError("AZURE_CONTAINER is not configured.")

    def _sign(self, object_key, permission, expires_in):
        """Sign a service SAS for one blob. Local, offline, no client involved."""
        self._require_container()
        if not self.account_key:
            raise ValueError(
                "AZURE_ACCOUNT_KEY is not configured; a service SAS cannot be signed "
                "without it."
            )

        return generate_blob_sas(
            account_name=self.account_name,
            container_name=self.container_name,
            blob_name=object_key,
            account_key=self.account_key,
            permission=permission,
            # No `start`: Azure treats an absent start as "valid on arrival", which
            # sidesteps clock skew between this host and the storage account entirely.
            expiry=datetime.now(timezone.utc) + expires_in,
        )

    def _format_file_size(self, size, unit="GB"):
        if size is None:
            return f"0.00 {unit or 'bytes'}"

        value = float(size)

        if unit == "GB":
            return f"{value / (1024 ** 3):.4f} GB"
        elif unit == "MB":
            return f"{value / (1024 ** 2):.2f} MB"
        elif unit == "KB":
            return f"{value / 1024:.2f} KB"
        elif unit == "bytes":
            return f"{int(value)} bytes"

        # Fallback to original auto-formatting
        units = ["bytes", "KB", "MB", "GB", "TB"]
        for u in units:
            if u == "bytes":
                if value < 1024:
                    return f"{int(value)} {u}"
            else:
                if value < 1024:
                    return f"{value:.2f} {u}"
            value /= 1024

        return f"{value:.2f} PB"

    # --- issuing signed URLs -------------------------------------------------

    def get_presigned_url_for_object(self, object_key, expiry_hours=1):
        """A read-only SAS URL for one blob.

        An ordinary HTTPS GET URL, which is what lets the ETL service consume it
        without knowing anything about Azure.
        """
        return self._with_public_endpoint(
            f"{self._blob_url(object_key)}?"
            + self._sign(
                object_key,
                BlobSasPermissions(read=True),
                timedelta(hours=expiry_hours),
            )
        )

    def get_presigned_put_url(
        self, object_key, content_type, file_size_in_bytes, expiry_minutes=15
    ):
        """A SAS URL the client PUTs the file body to, directly.

        Signed with **create**, not write. Azure refuses a Put Blob under ``create``
        when the blob already exists, so a leaked or replayed ticket cannot overwrite an
        object that has since been confirmed — a bound the presigned PUT this replaces
        did not have. It also means a client retrying a failed upload on the same ticket
        must be issued a fresh key, which the reserve step already does.

        ``file_size_in_bytes`` is accepted for call-site compatibility and is **not**
        enforced: a SAS carries no size condition (see the module docstring). The
        ceiling is checked at confirm, and an oversize object is deleted there.

        The caller must also send the headers from :meth:`get_upload_headers`.
        """
        return self._with_public_endpoint(
            f"{self._blob_url(object_key)}?"
            + self._sign(
                object_key,
                BlobSasPermissions(create=True),
                timedelta(minutes=expiry_minutes),
            )
        )

    def get_upload_headers(self, content_type=None):
        """Request headers a client must send with :meth:`get_presigned_put_url`.

        ``x-ms-blob-type`` is not optional and has no default — Azure rejects the PUT
        without it. Content-Type rides along here because a SAS cannot pin it; sending
        it is what gets the blob stored with the right type for later downloads.
        """
        headers = {BLOB_TYPE_HEADER: BLOCK_BLOB}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    # --- reading ------------------------------------------------------------

    def get_object_head_bytes(self, object_key, num_bytes=64):
        """Read the first ``num_bytes`` of a blob, and nothing more.

        A ranged read, so identifying a 50 MB recording costs 64 bytes of transfer
        rather than downloading it. That is what makes a content check affordable on
        every confirm.

        Returns ``b""`` when the blob is missing or empty; callers treat empty as
        "nothing to check" rather than as a failure. A blob *shorter* than the range is
        not an error on Azure — it returns what it has — but a zero-byte one answers
        ``InvalidRange``, and a file that short cannot carry a signature anyway.
        """
        self._require_container()

        try:
            downloader = self._blob(object_key).download_blob(
                offset=0, length=max(1, int(num_bytes))
            )
            return downloader.readall()
        except ResourceNotFoundError:
            return b""
        except HttpResponseError as e:
            if e.error_code in ("InvalidRange", "InvalidHeaderValue"):
                return b""
            logger.error(f"Failed to read head bytes of {object_key}: {e}")
            raise

    def verify_object_exists(self, object_key):
        """Whether the blob is actually there.

        Used to validate a direct client upload before a DB record starts trusting it.
        """
        self._require_container()

        try:
            return bool(self._blob(object_key).exists())
        except HttpResponseError as e:
            logger.error(f"Error verifying object {object_key} exists: {e}")
            raise

    def iter_objects(self, prefix):
        """Yield ``{key, size, last_modified}`` for every blob under ``prefix``.

        The SDK paginates internally, so a tenant with a hundred thousand blobs streams
        rather than materialising one list. Used by the orphan reconciliation, which is
        the only thing that can see objects the database has forgotten about.
        """
        self._require_container()

        container = self.client.get_container_client(self.container_name)
        for blob in container.list_blobs(name_starts_with=prefix):
            yield {
                "key": blob.name,
                "size": blob.size or 0,
                "last_modified": blob.last_modified,
            }

    def get_file_disk_usage(self, object_key):
        """Size and metadata for one blob.

        A missing blob is reported as zero rather than raised: the confirm flows call
        this to find out whether anything arrived, and "nothing did" is an answer.
        """
        self._require_container()

        try:
            props = self._blob(object_key).get_blob_properties()
        except ResourceNotFoundError:
            return {
                "total_size_bytes": 0,
                "formatted_size": "0 bytes",
                "content_type": None,
                "last_modified": None,
            }
        except HttpResponseError as e:
            logger.error(f"Failed to get file disk usage for {object_key}: {e}")
            raise

        size = getattr(props, "size", 0) or 0
        content_settings = getattr(props, "content_settings", None)
        return {
            "total_size_bytes": size,
            "formatted_size": self._format_file_size(size),
            "content_type": getattr(content_settings, "content_type", None)
            or "application/octet-stream",
            "last_modified": getattr(props, "last_modified", None),
        }

    # --- deleting ------------------------------------------------------------

    def delete_blob_direct(self, object_key):
        """Purge a blob from the configured container.

        A blob that is already gone counts as deleted. Azure raises where S3 returned
        success, and every caller here — the pending-upload sweep, both oversize
        rejection paths, the contact-import cleanup — is deleting something it does not
        know still exists. Propagating that would turn a no-op into a 500.
        """
        self._require_container()

        try:
            self._blob(object_key).delete_blob()
            logger.info(
                f"Directly deleted object: {object_key} from container "
                f"{self.container_name}"
            )
        except ResourceNotFoundError:
            logger.debug(
                f"Object {object_key} was already absent from container "
                f"{self.container_name}; treating delete as done."
            )
        except Exception as e:
            logger.error(f"Failed to delete object {object_key}: {e}")
            raise

        return True
