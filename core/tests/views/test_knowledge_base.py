from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from django.urls import reverse
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User
from core.models.knowledge_base import KnowledgeBase, KBText
from core.models.agent import Agent
from core.models.agent_template import AgentTemplate

class KnowledgeBaseViewTestCase(APITestCase):
    def setUp(self):
        # Create Tenants
        self.tenant = Tenant.objects.create(name="Acme Corp", subdomain="acme")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex")

        # Create Users
        self.user = User.objects.create_user(username="alex", email="alex@example.com", password="password")
        self.other_user = User.objects.create_user(username="bob", email="bob@example.com", password="password")
        self.etl_user = User.objects.create_user(
            username="etl_system",
            email="etl@example.com",
            password="password",
            user_type=User.UserType.ETL
        )

        # Establish Tenant Mappings
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.other_user, tenant=self.other_tenant)

        # M2M Component items
        self.text = KBText.objects.create(tenant=self.tenant, title="Docs Guide", description="Core info")

        # Knowledge Base Aggregate
        self.kb = KnowledgeBase.objects.create(tenant=self.tenant, name="Help Center")
        self.kb.texts.add(self.text)

        # Clients configured with authentication
        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)

        self.client_bob = APIClient()
        self.client_bob.force_authenticate(user=self.other_user)

        self.client_etl = APIClient()
        self.client_etl.credentials(HTTP_AUTHORIZATION="Bearer dummy_etl_system_token_for_local_dev")

    def test_kb_list_tenant_isolation(self):
        # Alex (Acme Corp) should see 1 Knowledge Base record
        response = self.client_alex.get("/api/v1/core/knowledge-base/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify paginated response structure from SetPagination
        self.assertTrue(response.data["status"])
        self.assertEqual(len(response.data["data"]), 1)
        self.assertEqual(response.data["data"][0]["name"], "Help Center")

        # Bob (Globex) should see 0 Knowledge Base records
        response_other = self.client_bob.get("/api/v1/core/knowledge-base/")
        self.assertEqual(response_other.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response_other.data["data"]), 0)

    def test_kb_retrieve_prefetched_nesting(self):
        # Retrieve Acme Knowledge Base details as Alex
        response = self.client_alex.get(f"/api/v1/core/knowledge-base/{self.kb.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        
        # Verify rich nested data prefetching for Many-to-Many fields
        kb_data = response.data["data"]
        self.assertEqual(len(kb_data["texts"]), 1)
        self.assertEqual(kb_data["texts"][0]["title"], "Docs Guide")

    def test_kb_dropdown_action(self):
        # Test Dropdown Picker action API
        response = self.client_alex.get("/api/v1/core/knowledge-base/dropdown/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        
        # Check picker options schema (label, value)
        dropdown_data = response.data["data"]
        self.assertEqual(len(dropdown_data), 1)
        self.assertEqual(dropdown_data[0]["label"], "Help Center")
        self.assertEqual(dropdown_data[0]["value"], str(self.kb.id))

    def test_kb_cascading_soft_delete(self):
        # Delete the central Knowledge Base Aggregate container
        response = self.client_alex.delete(f"/api/v1/core/knowledge-base/{self.kb.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])

        # Check soft-deleted statuses
        self.kb.refresh_from_db()
        self.assertTrue(self.kb.is_deleted)

        self.text.refresh_from_db()
        self.assertTrue(self.text.is_deleted)

    def test_kb_upload_file_endpoint(self):
        # Perform raw binary file upload through the custom multipart action
        good_file = SimpleUploadedFile("guide.pdf", b"pdf contents here", content_type="application/pdf")
        response = self.client_alex.post(
            "/api/v1/core/knowledge-base/upload_file/",
            {"file": good_file},
            format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["status"])
        
        # Verify response structure and schema
        file_data = response.data["data"]
        self.assertIn("id", file_data)
        self.assertEqual(file_data["original_name"], "guide.pdf")
        self.assertEqual(file_data["processing_status"], "PENDING")

    def test_kb_create_with_existing_file(self):
        from core.models.knowledge_base import KBFiles
        # 1. Create a KBFiles object
        uploaded_file = SimpleUploadedFile("guide.pdf", b"pdf contents", content_type="application/pdf")
        kb_file = KBFiles.objects.create(tenant=self.tenant, file=uploaded_file, original_name="guide.pdf")

        # 2. Call knowledge-base create endpoint linking this file
        payload = {
            "name": "New KB with existing file",
            "texts": [],
            "files": [
                {"id": str(kb_file.id)}
            ],
            "websites": [],
            "question_answers": []
        }
        response = self.client_alex.post(
            "/api/v1/core/knowledge-base/",
            payload,
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["status"])
        
        # Verify the file is associated
        kb = KnowledgeBase.objects.get(name="New KB with existing file")
        self.assertIn(kb_file, kb.files.all())

    def test_kb_update_etl_status_endpoint(self):
        import uuid
        etl_id_1 = uuid.uuid4()
        etl_id_2 = uuid.uuid4()

        # 1. Update KnowledgeBase itself
        payload = {
            "type": "knowledgebase",
            "object_id": str(self.kb.id),
            "etl_id": str(etl_id_1),
            "status": "PROCESSING",
        }
        response = self.client_etl.post(
            f"/api/v1/core/knowledge-base/{self.kb.id}/update-etl-status/",
            payload,
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.kb.refresh_from_db()
        self.assertEqual(self.kb.etl_id, etl_id_1)
        self.assertEqual(self.kb.processing_status, "PROCESSING")

        # 2. Try to update status with mismatching etl_id
        payload_mismatch = {
            "type": "knowledgebase",
            "object_id": str(self.kb.id),
            "etl_id": str(etl_id_2),
            "status": "COMPLETED",
        }
        response = self.client_etl.post(
            f"/api/v1/core/knowledge-base/{self.kb.id}/update-etl-status/",
            payload_mismatch,
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # 3. Update nested KBText
        payload_text = {
            "type": "text",
            "object_id": str(self.text.id),
            "etl_id": str(etl_id_1),
            "status": "COMPLETED",
        }
        response = self.client_etl.post(
            f"/api/v1/core/knowledge-base/{self.kb.id}/update-etl-status/",
            payload_text,
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.text.refresh_from_db()
        self.assertEqual(self.text.etl_id, etl_id_1)
        self.assertEqual(self.text.processing_status, "COMPLETED")

        # 4. Try updating an unassociated text object
        other_text = KBText.objects.create(tenant=self.tenant, title="Other Guide", description="Not in KB")
        payload_unassociated = {
            "type": "text",
            "object_id": str(other_text.id),
            "etl_id": str(etl_id_1),
            "status": "COMPLETED",
        }
        response = self.client_etl.post(
            f"/api/v1/core/knowledge-base/{self.kb.id}/update-etl-status/",
            payload_unassociated,
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_kb_update_etl_status_forbidden_for_non_etl_user(self):
        import uuid
        payload = {
            "type": "knowledgebase",
            "object_id": str(self.kb.id),
            "etl_id": str(uuid.uuid4()),
            "status": "PROCESSING",
        }
        response = self.client_alex.post(
            f"/api/v1/core/knowledge-base/{self.kb.id}/update-etl-status/",
            payload,
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_kb_delete_linked_validation(self):
        from core.models.agent import Agent
        from core.models.agent_template import AgentTemplate
        tpl = AgentTemplate.objects.create(
            tenant=self.tenant,
            name="Tpl",
            short_description="desc"
        )
        agent = Agent.objects.create(
            tenant=self.tenant,
            bot_name="Agent-Link-KB",
            persona_name="Alice",
            tone="PROFESSIONAL",
            agent_template=tpl,
        )
        agent.knowledge_bases.add(self.kb)
        
        # Delete should fail with 400 Warning
        response = self.client_alex.delete(f"/api/v1/core/knowledge-base/{self.kb.id}/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(response.data.get("warning"))
        
        # Bulk delete should fail with 400 Warning
        response_bulk = self.client_alex.post(
            "/api/v1/core/knowledge-base/bulk-delete/",
            {"ids": [str(self.kb.id)]},
            format="json"
        )
        self.assertEqual(response_bulk.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(response_bulk.data.get("warning"))
        
        # Delete with force=true should succeed
        response_force = self.client_alex.delete(f"/api/v1/core/knowledge-base/{self.kb.id}/?force=true")
        self.assertEqual(response_force.status_code, status.HTTP_200_OK)
        
        # Verify it is deleted
        self.kb.refresh_from_db()
        self.assertTrue(self.kb.is_deleted)
