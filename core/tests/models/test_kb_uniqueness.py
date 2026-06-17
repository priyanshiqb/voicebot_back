from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from core.models.knowledge_base import KnowledgeBase, KBWebsite

User = get_user_model()


class KBUniquenessTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.check_name_url = reverse("api-v1:core:knowledge-base-check-name")
        self.check_url_url = reverse("api-v1:core:knowledge-base-check-url")

        # Setup tenant & user
        self.tenant = Tenant.objects.create(name="Tenant Alpha", subdomain="tenant-alpha")
        self.user = User.objects.create_user(
            email="user@tenantalpha.com",
            password="SecurePassword123!",
            first_name="User",
            last_name="Alpha"
        )
        self.user.is_active = True
        self.user.save()
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)

        # Create existing KnowledgeBase & KBWebsite
        self.existing_kb = KnowledgeBase.objects.create(
            name="Alpha Base Guide",
            tenant=self.tenant
        )
        self.existing_website = KBWebsite.objects.create(
            url="https://alpha-docs.com",
            tenant=self.tenant
        )

        self.client.force_authenticate(user=self.user)

    def test_check_name_uniqueness_is_unique(self):
        """Verify check-name returns is_unique: true for a unique name."""
        payload = {"name": "Brand New Guide"}
        response = self.client.post(self.check_name_url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["data"]["is_unique"])

    def test_check_name_uniqueness_is_duplicate(self):
        """Verify check-name returns is_unique: false for an existing name."""
        payload = {"name": "Alpha Base Guide"}
        response = self.client.post(self.check_name_url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["data"]["is_unique"])

    def test_check_name_uniqueness_exclude_self(self):
        """Verify check-name returns is_unique: true when the owning ID is excluded."""
        payload = {
            "name": "Alpha Base Guide",
            "exclude_id": str(self.existing_kb.id)
        }
        response = self.client.post(self.check_name_url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["data"]["is_unique"])

    def test_check_url_uniqueness_is_unique(self):
        """Verify check-url returns is_unique: true for a unique URL."""
        payload = {"url": "https://new-docs.com"}
        response = self.client.post(self.check_url_url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["data"]["is_unique"])

    def test_check_url_uniqueness_is_duplicate(self):
        """Verify check-url returns is_unique: false for an existing URL."""
        payload = {"url": "https://alpha-docs.com"}
        response = self.client.post(self.check_url_url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["data"]["is_unique"])

    def test_check_url_uniqueness_exclude_self(self):
        """Verify check-url returns is_unique: true when the owning ID is excluded."""
        payload = {
            "url": "https://alpha-docs.com",
            "exclude_id": str(self.existing_website.id)
        }
        response = self.client.post(self.check_url_url, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["data"]["is_unique"])
