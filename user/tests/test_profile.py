from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from onboard.models.plan import Plan
from onboard.models.subscription import Subscription
from decimal import Decimal
from onboard.tests.billing_factories import make_plan

User = get_user_model()


class UserProfileEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.me_url = reverse("api-v1:customuser-me")

        # Create active user A (no tenant)
        self.user_no_tenant = User.objects.create_user(
            email="notenant@example.com",
            password="SecurePassword123!",
            first_name="No",
            last_name="Tenant"
        )
        self.user_no_tenant.is_active = True
        self.user_no_tenant.save()

        # Create active user B (has tenant, no subscription)
        self.user_with_tenant = User.objects.create_user(
            email="withtenant@example.com",
            password="SecurePassword123!",
            first_name="With",
            last_name="Tenant"
        )
        self.user_with_tenant.is_active = True
        self.user_with_tenant.save()

        self.tenant = Tenant.objects.create(
            name="Acme Corp",
            subdomain="acme"
        )
        self.mapping = TenantUserMapping.objects.create(
            user=self.user_with_tenant,
            tenant=self.tenant
        )

        # Create active user C (has tenant & active subscription)
        self.user_with_subscription = User.objects.create_user(
            email="subscribed@example.com",
            password="SecurePassword123!",
            first_name="Subscribed",
            last_name="User"
        )
        self.user_with_subscription.is_active = True
        self.user_with_subscription.save()

        self.tenant_sub = Tenant.objects.create(
            name="Subscribed Corp",
            subdomain="subscribed"
        )
        TenantUserMapping.objects.create(
            user=self.user_with_subscription,
            tenant=self.tenant_sub
        )

        # Through the factory: a plan needs a rate card before it has any price, and
        # `base_price` is translated into a band and a per-minute figure.
        self.plan = make_plan(
            "premium-yearly",
            name="Premium Plan",
            base_price=Decimal("100.00"),
            discount_percentage=Decimal("0.00"),
            billing_cycle="yearly",
        )

        self.subscription = Subscription.objects.create(
            tenant=self.tenant_sub,
            plan=self.plan,
            status=Subscription.SubscriptionStatusChoices.ACTIVE
        )

    def test_unauthenticated_profile_access_fails(self):
        """Verify that accessing `/me` without auth credentials returns 401."""
        response = self.client.get(self.me_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_profile_no_tenant(self):
        """Verify profile details when user is not associated with any tenant."""
        self.client.force_authenticate(user=self.user_no_tenant)
        response = self.client.get(self.me_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        data = response.data["data"]
        self.assertEqual(data["email"], "notenant@example.com")
        self.assertIsNone(data["tenant"])
        self.assertIsNone(data["subscription"])

    def test_profile_with_tenant_no_subscription(self):
        """Verify profile details when user has a tenant but no subscriptions."""
        self.client.force_authenticate(user=self.user_with_tenant)
        response = self.client.get(self.me_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        data = response.data["data"]
        self.assertEqual(data["email"], "withtenant@example.com")
        
        # Verify tenant details
        tenant_data = data["tenant"]
        self.assertIsNotNone(tenant_data)
        self.assertEqual(tenant_data["name"], "Acme Corp")
        self.assertEqual(tenant_data["subdomain"], "acme")
        
        # Verify subscription is None
        self.assertIsNone(data["subscription"])

    def test_profile_with_active_subscription(self):
        """Verify profile details when user has a tenant and an active subscription."""
        self.client.force_authenticate(user=self.user_with_subscription)
        response = self.client.get(self.me_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        data = response.data["data"]
        self.assertEqual(data["email"], "subscribed@example.com")
        
        # Verify tenant details
        tenant_data = data["tenant"]
        self.assertIsNotNone(tenant_data)
        self.assertEqual(tenant_data["name"], "Subscribed Corp")
        
        # Verify subscription details
        sub_data = data["subscription"]
        self.assertIsNotNone(sub_data)
        self.assertEqual(sub_data["status"], "active")
        
        # Verify plan details inside subscription
        plan_data = sub_data["plan"]
        self.assertIsNotNone(plan_data)
        self.assertEqual(plan_data["code"], "premium-yearly")
        self.assertEqual(plan_data["name"], self.plan.name)
        self.assertEqual(float(plan_data["net_base_price"]), 100.00)
