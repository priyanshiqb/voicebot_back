from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from django.urls import reverse
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User
from core.models.knowledge_base import KnowledgeBase, KBText

class KnowledgeBaseViewTestCase(APITestCase):
    def setUp(self):
        # Create Tenants
        self.tenant = Tenant.objects.create(name="Acme Corp", subdomain="acme")
        self.other_tenant = Tenant.objects.create(name="Globex", subdomain="globex")

        # Create Users
        self.user = User.objects.create_user(username="alex", email="alex@example.com", password="password")
        self.other_user = User.objects.create_user(username="bob", email="bob@example.com", password="password")

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

