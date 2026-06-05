from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping

User = get_user_model()


class UserTenantIsolationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.list_url = reverse("api-v1:customuser-list")

        # Create Tenant A & its users
        self.tenant_a = Tenant.objects.create(name="Tenant A", subdomain="tenant-a")
        
        self.manager_a = User.objects.create_user(
            email="manager_a@example.com",
            password="SecurePassword123!",
            first_name="Manager",
            last_name="A"
        )
        self.manager_a.is_active = True
        self.manager_a.save()
        TenantUserMapping.objects.create(user=self.manager_a, tenant=self.tenant_a)

        self.employee_a = User.objects.create_user(
            email="employee_a@example.com",
            password="SecurePassword123!",
            first_name="Employee",
            last_name="A"
        )
        self.employee_a.is_active = True
        self.employee_a.save()
        TenantUserMapping.objects.create(user=self.employee_a, tenant=self.tenant_a)

        # Create Tenant B & its users
        self.tenant_b = Tenant.objects.create(name="Tenant B", subdomain="tenant-b")
        
        self.manager_b = User.objects.create_user(
            email="manager_b@example.com",
            password="SecurePassword123!",
            first_name="Manager",
            last_name="B"
        )
        self.manager_b.is_active = True
        self.manager_b.save()
        TenantUserMapping.objects.create(user=self.manager_b, tenant=self.tenant_b)

        # Superuser
        self.superuser = User.objects.create_superuser(
            email="admin@example.com",
            password="SecurePassword123!",
            first_name="Super",
            last_name="User"
        )

    def test_list_isolation_for_regular_users(self):
        """Verify that a user in Tenant A can only list users belonging to Tenant A."""
        self.client.force_authenticate(user=self.manager_a)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # List should include manager_a and employee_a, but NOT manager_b
        emails = [item["email"] for item in response.data["data"]]
        self.assertIn("manager_a@example.com", emails)
        self.assertIn("employee_a@example.com", emails)
        self.assertNotIn("manager_b@example.com", emails)

    def test_retrieve_isolation_for_regular_users(self):
        """Verify that a user in Tenant A cannot retrieve detail profile of a user in Tenant B."""
        detail_url_b = reverse("api-v1:customuser-detail", args=[self.manager_b.id])
        
        self.client.force_authenticate(user=self.manager_a)
        response = self.client.get(detail_url_b)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_superuser_can_see_all_users(self):
        """Verify that a superuser can bypass tenant isolation filters and see all users."""
        self.client.force_authenticate(user=self.superuser)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        emails = [item["email"] for item in response.data["data"]]
        self.assertIn("manager_a@example.com", emails)
        self.assertIn("employee_a@example.com", emails)
        self.assertIn("manager_b@example.com", emails)
        self.assertIn("admin@example.com", emails)

    def test_create_user_automatically_assigns_tenant(self):
        """Verify that creating a user under Tenant A context automatically maps the new user to Tenant A."""
        self.client.force_authenticate(user=self.manager_a)
        
        payload = {
            "email": "new_user_a@example.com",
            "password": "SecurePassword123!",
            "first_name": "New",
            "last_name": "User",
            "phone_number": "+15555555555"
        }
        
        response = self.client.post(self.list_url, payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Check that the new user is created
        new_user = User.objects.get(email="new_user_a@example.com")
        
        # Verify the new user's mapping points to tenant_a
        mapping = TenantUserMapping.objects.get(user=new_user)
        self.assertEqual(mapping.tenant, self.tenant_a)

    def test_update_user_permissions_and_fields(self):
        """Verify that a user can update themselves, other users in the same tenant, but not users in other tenants, and can update timezone & language fields."""
        from onboard.models import Timezone, Language
        
        test_tz, _ = Timezone.objects.get_or_create(
            timezone="Asia/Kolkata",
            defaults={"offset": "UTC+05:30"}
        )
        test_lang, _ = Language.objects.get_or_create(
            code="en-IN",
            defaults={"name": "English (India)", "is_active": True}
        )

        detail_url_self = reverse("api-v1:customuser-detail", args=[self.manager_a.id])
        detail_url_same_tenant = reverse("api-v1:customuser-detail", args=[self.employee_a.id])
        detail_url_other_tenant = reverse("api-v1:customuser-detail", args=[self.manager_b.id])

        # 1. Authenticate as manager_a
        self.client.force_authenticate(user=self.manager_a)

        # 2. Update himself (should succeed and update timezone & language)
        payload = {
            "first_name": "Manager Updated",
            "timezone": str(test_tz.id),
            "language": str(test_lang.id)
        }
        response = self.client.patch(detail_url_self, payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify changes in DB
        self.manager_a.refresh_from_db()
        self.assertEqual(self.manager_a.first_name, "Manager Updated")
        self.assertEqual(self.manager_a.timezone, test_tz)
        self.assertEqual(self.manager_a.language, test_lang)

        # 3. Update another user in the same tenant (should succeed)
        payload_other = {
            "first_name": "Employee Updated"
        }
        response = self.client.patch(detail_url_same_tenant, payload_other)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        self.employee_a.refresh_from_db()
        self.assertEqual(self.employee_a.first_name, "Employee Updated")

        # 4. Update a user in a different tenant (should fail with 404)
        response = self.client.patch(detail_url_other_tenant, payload_other)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
