import os
import posixpath
import tempfile
import uuid
import mimetypes
import logging
from django.conf import settings
from django.core.files.storage import Storage
from azure.storage.blob import BlobServiceClient, ContentSettings, generate_blob_sas, BlobSasPermissions
from django.utils import timezone
from datetime import timedelta
from django.core.files.base import ContentFile
from utils.azure_blob.service import AzureStorageService

logger = logging.getLogger(__name__)


class AzureBlobDynamicUploadStorage(Storage):
    """
    Azure storage that dynamically resolves container
    based on instance.tenant_id
    """

    def __init__(self):
        self.client = BlobServiceClient.from_connection_string(
            settings.AZURE_CONNECTION_STRING
        )

    def get_container_name(self, name):
        name = name.replace("\\", "/")
        parts = name.split("/")
        
        # Fallback to default container if path has no directory prefix
        if len(parts) < 2:
            container = "global-uploads"
        else:
            container = parts[0]
            
        # Sanitize container name for Azure compatibility
        # 1. Lowercase
        container = container.lower()
        # 2. Replace underscores and other non-alphanumeric chars with hyphens
        container = container.replace("_", "-")
        container = "".join(c if c.isalnum() else "-" for c in container)
        # 3. Collapse multiple consecutive hyphens and strip leading/trailing hyphens
        container = "-".join(filter(None, container.split("-")))
        
        # 4. Enforce length constraints (3 to 63 characters)
        if len(container) < 3:
            container = f"ctx-{container}"
        elif len(container) > 63:
            container = container[:63]
            
        return container

    def _save(self, name, content):
        """
        name → comes from upload_to function
        content → uploaded file
        Example name: tenant_id/folder/sub-folder/...../file.pdf
        """
        # Django passes name as upload_to result
        # Example: tenant_id/kb_files/file.pdf
        name = name.replace("\\", "/")

        container_name = self.get_container_name(name)

        container_client = self.client.get_container_client(container_name)

        if not container_client.exists():
            client = AzureStorageService()
            container_client = client.create_container(container_name)

        # Split path & filename
        directory, filename = posixpath.split(name)
        base, ext = posixpath.splitext(filename)
        
        blob_name = name

        blob_client = container_client.get_blob_client(blob_name)

        # If file already exists → append timestamp
        if blob_client.exists():
            timestamp = timezone.now().strftime("%Y%m%d%H%M%S")
            random_str = uuid.uuid4().hex[:6]
            filename = f"{base}_{timestamp}_{random_str}{ext}"
            
            # Avoid leading slash if directory is empty
            if directory:
                blob_name = f"{directory}/{filename}"
            else:
                blob_name = filename
                
            blob_client = container_client.get_blob_client(blob_name)

        content_type, _ = mimetypes.guess_type(blob_name)

        # Ensure content is read from the beginning
        if hasattr(content, "seek"):
            content.seek(0)

        blob_client.upload_blob(
            content,
            overwrite=False,
            content_settings=ContentSettings(
                content_type=content_type or "application/octet-stream"
            ),
        )

        return blob_name

    def url(self, blob_name):
        return blob_name

    def exists(self, name):
        """
        Check if blob exists in Azure.
        Required by Django Storage API.
        """
        container_name = self.get_container_name(name)

        blob_client = self.client.get_blob_client(
            container=container_name,
            blob=name,
        )

        return blob_client.exists()
    
    def path(self, name):
        logger.info(f"Generating URL for blob: {name}")
        return self.url(name)
    
    def size(self, name):
        container_name = self.get_container_name(name)

        blob_client = self.client.get_blob_client(
            container=container_name,
            blob=name,
        )

        properties = blob_client.get_blob_properties()
        return properties.size
    
    def open(self, name, mode="rb"):
        container_name = self.get_container_name(name)

        blob_client = self.client.get_blob_client(
            container=container_name,
            blob=name,
        )

        stream = blob_client.download_blob()
        return ContentFile(stream.read())
    
    def delete(self, name):
        container_name = self.get_container_name(name)

        blob_client = self.client.get_blob_client(
            container=container_name,
            blob=name,
        )

        try:
            blob_client.delete_blob()
            logger.info(f"Deleted blob: {name}")
        except Exception as e:
            logger.warning(f"Failed to delete blob: {name}, error: {e}")
            pass  # safe ignore
