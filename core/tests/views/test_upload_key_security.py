"""
Regression tests for the cross-tenant presigned-upload hole.

Both the Knowledge Base and Call Recording flows used to accept whatever object key
the client posted back to confirm-upload, verifying only that the object existed. That
allowed a caller to (a) confirm another tenant's object into their own record and read
it via the presigned GET URL, and (b) destroy another tenant's object through the
oversize-rejection branch, which deleted the key it was given before any tenant was
resolved.
"""

from unittest.mock import patch

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.agent import Agent
from core.models.agent_template import AgentTemplate
from core.models.agent_call_recording import AgentCallRecording
from core.models.knowledge_base import KBFiles
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User
from utils.s3_storage.keys import build_tenant_object_key


class UploadKeyOwnershipTests(APITestCase):
    """confirm-upload must reject keys outside the caller's tenant namespace."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme Corp", subdomain="acme-sec")
        self.victim_tenant = Tenant.objects.create(name="Globex", subdomain="globex-sec")

        self.user = User.objects.create_user(
            username="attacker", email="attacker@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)

        self.client_attacker = APIClient()
        self.client_attacker.force_authenticate(user=self.user)

        # A key that legitimately belongs to the victim tenant.
        self.victim_kb_key = build_tenant_object_key(
            self.victim_tenant.id, "kb_files", "confidential.pdf"
        )
        self.victim_recording_key = build_tenant_object_key(
            self.victim_tenant.id, "call_recordings", "private_call.mp3"
        )

    # ---------- Knowledge Base ----------

    @patch("core.views.knowledge_base.S3StorageService")
    def test_kb_confirm_rejects_cross_tenant_key(self, mock_s3):
        """Confirming another tenant's key must 404 and create no record."""
        response = self.client_attacker.post(
            "/api/v1/core/knowledge-base/confirm-upload/",
            {"object_key": self.victim_kb_key, "original_name": "confidential.pdf"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(KBFiles.all_objects.exists())
        # S3 must not even be contacted for a key we do not own.
        mock_s3.return_value.verify_object_exists.assert_not_called()

    @patch("core.views.knowledge_base.S3StorageService")
    def test_kb_confirm_cross_tenant_oversize_does_not_delete(self, mock_s3):
        """
        The oversize branch deletes the key it is handed. Authorization must run
        first, or an attacker can purge another tenant's file without reading it.
        """
        mock_s3.return_value.verify_object_exists.return_value = True
        mock_s3.return_value.get_file_disk_usage.return_value = {
            "total_size_bytes": 500 * 1024 * 1024
        }

        response = self.client_attacker.post(
            "/api/v1/core/knowledge-base/confirm-upload/",
            {"object_key": self.victim_kb_key, "original_name": "confidential.pdf"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        mock_s3.return_value.delete_blob_direct.assert_not_called()

    @patch("core.views.knowledge_base.S3StorageService")
    def test_kb_confirm_accepts_own_key(self, mock_s3):
        """The legitimate path still works."""
        mock_s3.return_value.verify_object_exists.return_value = True
        mock_s3.return_value.get_file_disk_usage.return_value = {"total_size_bytes": 1024}

        own_key = build_tenant_object_key(self.tenant.id, "kb_files", "mine.pdf")
        response = self.client_attacker.post(
            "/api/v1/core/knowledge-base/confirm-upload/",
            {"object_key": own_key, "original_name": "mine.pdf"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(KBFiles.all_objects.count(), 1)
        self.assertEqual(KBFiles.all_objects.first().tenant_id, self.tenant.id)

    @patch("core.views.knowledge_base.S3StorageService")
    def test_kb_confirm_rejects_traversal_key(self, mock_s3):
        """A key that walks out of the tenant prefix must be rejected."""
        traversal = f"t-{self.tenant.id}/kb_files/../../t-{self.victim_tenant.id}/kb_files/x.pdf"
        response = self.client_attacker.post(
            "/api/v1/core/knowledge-base/confirm-upload/",
            {"object_key": traversal, "original_name": "x.pdf"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(KBFiles.all_objects.exists())

    @patch("core.views.knowledge_base.S3StorageService")
    def test_kb_request_url_sanitizes_filename(self, mock_s3):
        """A crafted filename must not steer the generated key out of the prefix."""
        mock_s3.return_value.get_presigned_put_url.return_value = "https://example/put"

        response = self.client_attacker.post(
            "/api/v1/core/knowledge-base/request-upload-url/",
            {
                "filename": f"../../t-{self.victim_tenant.id}/kb_files/evil.pdf",
                "content_type": "application/pdf",
                "file_size_in_bytes": 1024,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        object_key = response.data["data"]["object_key"]
        self.assertTrue(object_key.startswith(f"t-{self.tenant.id}/kb_files/"))
        self.assertNotIn("..", object_key)
        self.assertNotIn(str(self.victim_tenant.id), object_key)

    # ---------- Call Recordings ----------

    @patch("core.views.agent_call_log.S3StorageService")
    def test_recording_confirm_rejects_cross_tenant_key(self, mock_s3):
        response = self.client_attacker.post(
            "/api/v1/core/agent-call-logs/confirm-upload/",
            {"object_key": self.victim_recording_key, "original_name": "private_call.mp3"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(AgentCallRecording.all_objects.exists())
        mock_s3.return_value.verify_object_exists.assert_not_called()

    @patch("core.views.agent_call_log.S3StorageService")
    def test_recording_confirm_cross_tenant_oversize_does_not_delete(self, mock_s3):
        mock_s3.return_value.verify_object_exists.return_value = True
        mock_s3.return_value.get_file_disk_usage.return_value = {
            "total_size_bytes": 500 * 1024 * 1024
        }

        response = self.client_attacker.post(
            "/api/v1/core/agent-call-logs/confirm-upload/",
            {"object_key": self.victim_recording_key, "original_name": "private_call.mp3"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        mock_s3.return_value.delete_blob_direct.assert_not_called()

    @patch("core.views.agent_call_log.S3StorageService")
    def test_recording_confirm_accepts_own_key(self, mock_s3):
        mock_s3.return_value.verify_object_exists.return_value = True
        mock_s3.return_value.get_file_disk_usage.return_value = {"total_size_bytes": 2048}

        own_key = build_tenant_object_key(self.tenant.id, "call_recordings", "mine.mp3")
        response = self.client_attacker.post(
            "/api/v1/core/agent-call-logs/confirm-upload/",
            {"object_key": own_key, "original_name": "mine.mp3"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(AgentCallRecording.all_objects.count(), 1)
        self.assertEqual(
            AgentCallRecording.all_objects.first().tenant_id, self.tenant.id
        )


class FileIdUploadPathTests(APITestCase):
    """
    Stage 2: confirm by `file_id` so the object key never crosses the wire.

    The deprecated `object_key` body is still accepted during the transition window,
    so both paths are exercised here.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme Corp", subdomain="acme-fid")
        self.victim_tenant = Tenant.objects.create(name="Globex", subdomain="globex-fid")

        self.user = User.objects.create_user(
            username="fid-user", email="fid@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        self.client_user = APIClient()
        self.client_user.force_authenticate(user=self.user)

    @patch("core.views.knowledge_base.S3StorageService")
    def test_kb_request_url_returns_file_id_and_reserves_a_pending_row(self, mock_s3):
        mock_s3.return_value.get_presigned_put_url.return_value = "https://example/put"

        response = self.client_user.post(
            "/api/v1/core/knowledge-base/request-upload-url/",
            {
                "filename": "guide.pdf",
                "content_type": "application/pdf",
                "file_size_in_bytes": 1024,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        file_id = response.data["data"]["file_id"]

        kb_file = KBFiles.all_objects.get(id=file_id)
        self.assertEqual(kb_file.upload_status, KBFiles.UploadStatus.PENDING)
        self.assertEqual(kb_file.tenant_id, self.tenant.id)

    @patch("core.views.knowledge_base.S3StorageService")
    def test_kb_confirm_by_file_id_reuses_the_row(self, mock_s3):
        mock_s3.return_value.get_presigned_put_url.return_value = "https://example/put"
        mock_s3.return_value.verify_object_exists.return_value = True
        mock_s3.return_value.get_file_disk_usage.return_value = {"total_size_bytes": 2048}

        requested = self.client_user.post(
            "/api/v1/core/knowledge-base/request-upload-url/",
            {
                "filename": "guide.pdf",
                "content_type": "application/pdf",
                "file_size_in_bytes": 1024,
            },
            format="json",
        )
        file_id = requested.data["data"]["file_id"]

        confirmed = self.client_user.post(
            "/api/v1/core/knowledge-base/confirm-upload/",
            {"file_id": file_id},
            format="json",
        )

        self.assertEqual(confirmed.status_code, status.HTTP_201_CREATED)
        # No second row: the reserved one is promoted, not duplicated.
        self.assertEqual(KBFiles.all_objects.count(), 1)

        kb_file = KBFiles.all_objects.get(id=file_id)
        self.assertEqual(kb_file.upload_status, KBFiles.UploadStatus.CONFIRMED)
        self.assertEqual(kb_file.file_size_in_bytes, 2048)

    @patch("core.views.knowledge_base.S3StorageService")
    def test_kb_confirm_by_another_tenants_file_id_is_404(self, mock_s3):
        victim_file = KBFiles.objects.create(
            tenant=self.victim_tenant,
            original_name="secret.pdf",
            upload_status=KBFiles.UploadStatus.PENDING,
        )

        response = self.client_user.post(
            "/api/v1/core/knowledge-base/confirm-upload/",
            {"file_id": str(victim_file.id)},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        mock_s3.return_value.verify_object_exists.assert_not_called()

    def test_kb_confirm_requires_file_id_or_object_key(self):
        response = self.client_user.post(
            "/api/v1/core/knowledge-base/confirm-upload/", {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("core.views.agent_call_log.S3StorageService")
    def test_recording_confirm_by_file_id_reuses_the_row(self, mock_s3):
        mock_s3.return_value.get_presigned_put_url.return_value = "https://example/put"
        mock_s3.return_value.verify_object_exists.return_value = True
        mock_s3.return_value.get_file_disk_usage.return_value = {"total_size_bytes": 4096}

        requested = self.client_user.post(
            "/api/v1/core/agent-call-logs/request-upload-url/",
            {
                "filename": "call.mp3",
                "content_type": "audio/mpeg",
                "file_size_in_bytes": 1024,
            },
            format="json",
        )
        self.assertEqual(requested.status_code, status.HTTP_200_OK)
        file_id = requested.data["data"]["file_id"]

        confirmed = self.client_user.post(
            "/api/v1/core/agent-call-logs/confirm-upload/",
            {"file_id": file_id},
            format="json",
        )

        self.assertEqual(confirmed.status_code, status.HTTP_201_CREATED)
        self.assertEqual(AgentCallRecording.all_objects.count(), 1)

        recording = AgentCallRecording.all_objects.get(id=file_id)
        self.assertEqual(
            recording.upload_status, AgentCallRecording.UploadStatus.CONFIRMED
        )
        self.assertEqual(recording.file_size_in_bytes, 4096)

    @patch("core.views.agent_call_log.S3StorageService")
    def test_recording_confirm_by_another_tenants_file_id_is_404(self, mock_s3):
        victim = AgentCallRecording.objects.create(
            tenant=self.victim_tenant,
            original_name="private.mp3",
            upload_status=AgentCallRecording.UploadStatus.PENDING,
        )

        response = self.client_user.post(
            "/api/v1/core/agent-call-logs/confirm-upload/",
            {"file_id": str(victim.id)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("core.views.knowledge_base.S3StorageService")
    def test_legacy_object_key_path_still_works(self, mock_s3):
        """The transition window must not break clients that echo object_key back."""
        mock_s3.return_value.verify_object_exists.return_value = True
        mock_s3.return_value.get_file_disk_usage.return_value = {"total_size_bytes": 512}

        key = build_tenant_object_key(self.tenant.id, "kb_files", "legacy.pdf")
        response = self.client_user.post(
            "/api/v1/core/knowledge-base/confirm-upload/",
            {"object_key": key, "original_name": "legacy.pdf"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(KBFiles.all_objects.count(), 1)
        self.assertEqual(
            KBFiles.all_objects.first().upload_status, KBFiles.UploadStatus.CONFIRMED
        )

    def test_legacy_path_still_requires_original_name(self):
        response = self.client_user.post(
            "/api/v1/core/knowledge-base/confirm-upload/",
            {"object_key": build_tenant_object_key(self.tenant.id, "kb_files", "x.pdf")},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VoicebotAgentUploadPathTests(APITestCase):
    """
    The VB_AGENT path resolves its tenant from the agent, not from a tenant mapping.
    Authorizing against request.user.tenant_mapping would silently break AI-team
    uploads, so this guards the reordering done in the fix.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme Corp", subdomain="acme-vb")
        self.template = AgentTemplate.objects.create(
            tenant=self.tenant, name="Template VB"
        )
        self.agent = Agent.objects.create(
            tenant=self.tenant, bot_name="Bot VB", agent_template=self.template
        )

        # A VB_AGENT user deliberately has no TenantUserMapping.
        self.vb_user = User.objects.create_user(
            username="vb_agent",
            email="vb@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT,
        )
        self.client_vb = APIClient()
        self.client_vb.force_authenticate(user=self.vb_user)

    @patch("core.views.agent_call_log.S3StorageService")
    def test_vb_agent_request_url_uses_agent_tenant(self, mock_s3):
        mock_s3.return_value.get_presigned_put_url.return_value = "https://example/put"

        response = self.client_vb.post(
            "/api/v1/core/agent-call-logs/request-upload-url/",
            {
                "filename": "call.mp3",
                "content_type": "audio/mpeg",
                "file_size_in_bytes": 1024,
                "agent": str(self.agent.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            response.data["data"]["object_key"].startswith(
                f"t-{self.tenant.id}/call_recordings/"
            )
        )

    @patch("core.views.agent_call_log.S3StorageService")
    def test_vb_agent_confirm_upload_still_works(self, mock_s3):
        mock_s3.return_value.verify_object_exists.return_value = True
        mock_s3.return_value.get_file_disk_usage.return_value = {"total_size_bytes": 2048}

        key = build_tenant_object_key(self.tenant.id, "call_recordings", "call.mp3")
        response = self.client_vb.post(
            "/api/v1/core/agent-call-logs/confirm-upload/",
            {
                "object_key": key,
                "original_name": "call.mp3",
                "agent": str(self.agent.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            AgentCallRecording.all_objects.first().tenant_id, self.tenant.id
        )

    @patch("core.views.agent_call_log.S3StorageService")
    def test_vb_agent_cannot_confirm_other_tenant_key(self, mock_s3):
        other = Tenant.objects.create(name="Globex", subdomain="globex-vb")
        key = build_tenant_object_key(other.id, "call_recordings", "private.mp3")

        response = self.client_vb.post(
            "/api/v1/core/agent-call-logs/confirm-upload/",
            {
                "object_key": key,
                "original_name": "private.mp3",
                "agent": str(self.agent.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(AgentCallRecording.all_objects.exists())
        mock_s3.return_value.delete_blob_direct.assert_not_called()
