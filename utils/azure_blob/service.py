import re
import posixpath
import logging
from datetime import datetime, timezone, timedelta
from django.conf import settings
from azure.storage.blob import (
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
    generate_container_sas,
    BlobSasPermissions,
    ContainerSasPermissions,
)
from azure.core.exceptions import ResourceExistsError

logger = logging.getLogger(__name__)


class AzureStorageService:
    """
    Service client helper to interact with Azure Blob Storage API
    """

    def __init__(self):
        self.client = BlobServiceClient.from_connection_string(
            settings.AZURE_CONNECTION_STRING
        )

    def _parse_connection_string(self):
        """
        Parses connection string to extract account name and key safely.
        """
        conn_str = settings.AZURE_CONNECTION_STRING
        account_name = None
        account_key = None

        pairs = conn_str.split(";")
        for pair in pairs:
            if "=" in pair:
                key, val = pair.split("=", 1)
                key_lower = key.strip().lower()
                if key_lower == "accountname":
                    account_name = val.strip()
                elif key_lower == "accountkey":
                    account_key = val.strip()

        return account_name, account_key

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

    def create_container(self, container_name):
        """
        Creates a container if it doesn't already exist and returns its client.
        Safeguarded against concurrent creation races.
        """
        try:
            container_client = self.client.get_container_client(container_name)
            if not container_client.exists():
                try:
                    container_client.create_container()
                    logger.info(f"Created Azure container: {container_name}")
                except ResourceExistsError:
                    logger.info(f"Azure container {container_name} already created by concurrent request.")
            return container_client
        except Exception as e:
            logger.error(f"Failed to create Azure container {container_name}: {e}")
            raise

    def generate_blob_sas_url(self, container_name, blob_name, expiry_hours=1):
        """
        Generates a temporary, signed read-only URL (Shared Access Signature) for a single private blob.
        """
        try:
            account_name, account_key = self._parse_connection_string()
            if not account_name or not account_key:
                raise ValueError("Invalid Azure connection string: missing account details.")

            sas_token = generate_blob_sas(
                account_name=account_name,
                container_name=container_name,
                blob_name=blob_name,
                account_key=account_key,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
            )

            base_url = self.client.url.rstrip("/")
            return f"{base_url}/{container_name}/{blob_name}?{sas_token}"
        except Exception as e:
            logger.error(f"Failed to generate SAS URL for blob {blob_name} in {container_name}: {e}")
            raise

    def generate_container_sas_url(self, container_name, expiry_hours=1):
        """
        Generates a Shared Access Signature token/URL for an entire container.
        """
        try:
            account_name, account_key = self._parse_connection_string()
            if not account_name or not account_key:
                raise ValueError("Invalid Azure connection string: missing account details.")

            sas_token = generate_container_sas(
                account_name=account_name,
                container_name=container_name,
                account_key=account_key,
                permission=ContainerSasPermissions(read=True, write=True, list=True),
                expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
            )

            base_url = self.client.url.rstrip("/")
            return f"{base_url}/{container_name}?{sas_token}"
        except Exception as e:
            logger.error(f"Failed to generate SAS URL for container {container_name}: {e}")
            raise

    def upload_file_data(self, container_name, blob_name, data, content_type="application/octet-stream", make_unique=True):
        """
        Directly uploads raw bytes or string data to a target blob location.
        """
        try:
            container_client = self.create_container(container_name)

            directory, filename = posixpath.split(blob_name)
            base, ext = posixpath.splitext(filename)

            blob_client = container_client.get_blob_client(blob_name)

            if make_unique and blob_client.exists():
                from django.utils import timezone as django_timezone
                import uuid
                timestamp = django_timezone.now().strftime("%Y%m%d%H%M%S")
                random_str = uuid.uuid4().hex[:6]
                filename = f"{base}_{timestamp}_{random_str}{ext}"

                if directory:
                    blob_name = f"{directory}/{filename}"
                else:
                    blob_name = filename

                blob_client = container_client.get_blob_client(blob_name)

            blob_client.upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )
            return blob_name
        except Exception as e:
            logger.error(f"Failed to upload data to blob {blob_name} in {container_name}: {e}")
            raise

    def get_container_storage_usage(self, container_name):
        """
        Iterates through all blobs inside the container to calculate the exact storage size and count.
        """
        try:
            container_client = self.client.get_container_client(container_name)
            if not container_client.exists():
                return {
                    "total_size_bytes": 0,
                    "formatted_size": "0 bytes",
                    "blob_count": 0,
                }

            total_size = 0
            count = 0
            blobs = container_client.list_blobs()
            for blob in blobs:
                total_size += blob.size
                count += 1

            return {
                "total_size_bytes": total_size,
                "formatted_size": self._format_file_size(total_size),
                "blob_count": count,
            }
        except Exception as e:
            logger.error(f"Failed to calculate storage usage for container {container_name}: {e}")
            raise

    def delete_blob_direct(self, container_name, blob_name):
        """
        Directly purges a blob from the container.
        """
        try:
            container_client = self.client.get_container_client(container_name)
            if container_client.exists():
                blob_client = container_client.get_blob_client(blob_name)
                if blob_client.exists():
                    blob_client.delete_blob()
                    logger.info(f"Directly deleted blob: {blob_name} in container: {container_name}")
                    return True
            return False
        except Exception as e:
            logger.error(f"Failed to delete blob {blob_name} in container {container_name}: {e}")
            raise
