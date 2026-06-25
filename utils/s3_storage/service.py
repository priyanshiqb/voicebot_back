import logging
import posixpath
import mimetypes
from datetime import datetime, timezone, timedelta
from django.conf import settings
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

class S3StorageService:
    """
    Service client helper to interact with S3-compatible APIs (MinIO, AWS S3, Cloudflare R2).
    """

    def __init__(self):
        # We use standard boto3 configuration from Django settings
        self.endpoint_url = getattr(settings, "S3_ENDPOINT_URL", None)
        self.access_key = getattr(settings, "S3_ACCESS_KEY_ID", None)
        self.secret_key = getattr(settings, "S3_SECRET_ACCESS_KEY", None)
        self.region_name = getattr(settings, "S3_REGION_NAME", "us-east-1")
        self.bucket_name = getattr(settings, "S3_BUCKET_NAME", None)
        self.custom_domain = getattr(settings, "S3_CUSTOM_DOMAIN", None)

        if not self.access_key or not self.secret_key or not self.bucket_name:
            logger.warning("S3StorageService initialized without fully configuring S3_BUCKET_NAME or credentials.")

        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region_name,
            config=Config(signature_version="s3v4")
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

    def create_bucket_if_not_exists(self, bucket_name=None):
        """
        Checks and creates the configured S3 bucket if it doesn't already exist.
        """
        target_bucket = bucket_name or self.bucket_name
        if not target_bucket:
            raise ValueError("No bucket name provided or configured in settings.")

        try:
            # Check if bucket exists
            self.client.head_bucket(Bucket=target_bucket)
            logger.debug(f"Bucket {target_bucket} already exists.")
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == '404':
                try:
                    if self.region_name and self.region_name != 'us-east-1':
                        location = {'LocationConstraint': self.region_name}
                        self.client.create_bucket(Bucket=target_bucket, CreateBucketConfiguration=location)
                    else:
                        self.client.create_bucket(Bucket=target_bucket)
                    logger.info(f"Successfully created S3 bucket: {target_bucket}")
                except ClientError as create_error:
                    logger.error(f"Failed to create S3 bucket {target_bucket}: {create_error}")
                    raise
            else:
                logger.error(f"Error checking bucket {target_bucket}: {e}")
                raise

    def get_presigned_url_for_object(self, object_key, expiry_hours=1):
        """
        Generates a presigned URL to securely access or download a specific file object.
        """
        if not self.bucket_name:
            raise ValueError("S3_BUCKET_NAME is not configured.")

        try:
            url = self.client.generate_presigned_url(
                ClientMethod='get_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': object_key
                },
                ExpiresIn=expiry_hours * 3600
            )
            
            # If a custom domain is configured (e.g. for localhost routing), replace the internal endpoint
            if self.custom_domain:
                parsed_url = urlparse(url)
                # Parse scheme and netloc from custom domain
                custom_parsed = urlparse(self.custom_domain)
                url = url.replace(f"{parsed_url.scheme}://{parsed_url.netloc}", f"{custom_parsed.scheme}://{custom_parsed.netloc}")
                
            return url
        except ClientError as e:
            logger.error(f"Failed to generate presigned URL for {object_key}: {e}")
            raise

    def get_presigned_put_url(self, object_key, content_type, file_size_in_bytes, expiry_minutes=15):
        """
        Generates a presigned URL for direct frontend PUT uploads.
        Enforces an exact ContentLength to prevent malicious large file uploads directly to S3.
        """
        if not self.bucket_name:
            raise ValueError("S3_BUCKET_NAME is not configured.")

        try:
            url = self.client.generate_presigned_url(
                ClientMethod='put_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': object_key,
                    'ContentType': content_type,
                    # 'ContentLength': file_size_in_bytes,
                },
                ExpiresIn=expiry_minutes * 60
            )
            
            if self.custom_domain:
                parsed_url = urlparse(url)
                custom_parsed = urlparse(self.custom_domain)
                url = url.replace(f"{parsed_url.scheme}://{parsed_url.netloc}", f"{custom_parsed.scheme}://{custom_parsed.netloc}")
                
            return url
        except ClientError as e:
            logger.error(f"Failed to generate presigned PUT URL for {object_key}: {e}")
            raise

    def verify_object_exists(self, object_key):
        """
        Verifies that an object actually exists in the S3 bucket via a HEAD request.
        Used to securely validate direct frontend uploads before creating DB records.
        """
        if not self.bucket_name:
            raise ValueError("S3_BUCKET_NAME is not configured.")
            
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=object_key)
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                return False
            logger.error(f"Error verifying object {object_key} exists: {e}")
            raise

    def get_presigned_url_for_folder(self, folder_prefix, expiry_hours=1):
        """
        Generates a presigned URL/token with policy scoping for a specific folder prefix.
        Note: Standard S3 APIs generate presigned URLs for single objects or specific operations.
        For uploading into a folder dynamically, typically a presigned POST is used.
        Here we generate a presigned URL for 'list_objects_v2' constrained by a prefix,
        which is functionally useful for ETLs scanning a folder.
        """
        if not self.bucket_name:
            raise ValueError("S3_BUCKET_NAME is not configured.")

        if not folder_prefix.endswith('/'):
            folder_prefix += '/'

        try:
            url = self.client.generate_presigned_url(
                ClientMethod='list_objects_v2',
                Params={
                    'Bucket': self.bucket_name,
                    'Prefix': folder_prefix
                },
                ExpiresIn=expiry_hours * 3600
            )
            
            # If a custom domain is configured (e.g. for localhost routing), replace the internal endpoint
            if self.custom_domain:
                parsed_url = urlparse(url)
                custom_parsed = urlparse(self.custom_domain)
                url = url.replace(f"{parsed_url.scheme}://{parsed_url.netloc}", f"{custom_parsed.scheme}://{custom_parsed.netloc}")
                
            return url
        except ClientError as e:
            logger.error(f"Failed to generate presigned URL for folder {folder_prefix}: {e}")
            raise

    def get_folder_disk_usage(self, folder_prefix):
        """
        Calculates the total disk space and object count within a given folder prefix.
        """
        if not self.bucket_name:
            raise ValueError("S3_BUCKET_NAME is not configured.")

        total_size = 0
        count = 0

        try:
            paginator = self.client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=self.bucket_name, Prefix=folder_prefix)

            for page in pages:
                if 'Contents' in page:
                    for obj in page['Contents']:
                        total_size += obj['Size']
                        count += 1

            return {
                "total_size_bytes": total_size,
                "formatted_size": self._format_file_size(total_size),
                "object_count": count,
            }
        except ClientError as e:
            logger.error(f"Failed to calculate storage usage for folder {folder_prefix}: {e}")
            raise

    def get_file_disk_usage(self, object_key):
        """
        Returns the disk size and metadata for a specific file object.
        """
        if not self.bucket_name:
            raise ValueError("S3_BUCKET_NAME is not configured.")

        try:
            response = self.client.head_object(Bucket=self.bucket_name, Key=object_key)
            size = response.get('ContentLength', 0)
            return {
                "total_size_bytes": size,
                "formatted_size": self._format_file_size(size),
                "content_type": response.get('ContentType', 'application/octet-stream'),
                "last_modified": response.get('LastModified'),
            }
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                return {
                    "total_size_bytes": 0,
                    "formatted_size": "0 bytes",
                    "content_type": None,
                    "last_modified": None,
                }
            logger.error(f"Failed to get file disk usage for {object_key}: {e}")
            raise

    def delete_blob_direct(self, object_key):
        """
        Directly purges an object from the configured bucket.
        """
        if not self.bucket_name:
            raise ValueError("S3_BUCKET_NAME is not configured.")

        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=object_key)
            logger.info(f"Directly deleted object: {object_key} from bucket {self.bucket_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete object {object_key}: {e}")
            raise
