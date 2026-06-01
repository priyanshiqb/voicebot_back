from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from onboard.models.plan import Plan
from user.models import CustomUser
from decimal import Decimal


class PlanTests(APITestCase):
    """
    Unit and integration tests for the Plan model and PlanViewSet endpoints.
    """

    def setUp(self):
        # Create a test user and a superuser
        self.user = CustomUser.objects.create_user(
            email="testuser@example.com",
            password="testpassword123",
            first_name="Test",
            last_name="User"
        )
        self.superuser = CustomUser.objects.create_superuser(
            email="admin@example.com",
            password="adminpassword123",
            username="admin@example.com"
        )
        self.client.force_authenticate(user=self.superuser)

        # Create a default plan for testing list/retrieve operations
        self.default_plan = Plan.objects.create(
            code="starter_plan",
            name="Starter Plan",
            description="Perfect for individuals",
            base_price=Decimal("100.00"),
            discount_percentage=Decimal("10.00"),  # 10% discount
            tax_percentage=Decimal("18.00"),       # 18% tax
            currency=Plan.CurrencyChoices.INR,
            billing_cycle=Plan.BillingCycleChoices.MONTHLY,
            plan_type=Plan.PlanTypeChoices.PAID,
            sort_order=1,
            is_active=True
        )

        self.list_url = reverse("api-v1:plan-list")
        self.dropdown_url = reverse("api-v1:plan-dropdown")
        self.detail_url = reverse("api-v1:plan-detail", kwargs={"pk": self.default_plan.id})

    def test_plan_properties(self):
        """
        Test that computed properties on the Plan model calculate correctly.
        """
        plan = self.default_plan

        # gross base_price = 100.00
        # discount_percentage = 10.00 -> discount_amount = 10.00
        self.assertEqual(plan.discount_amount, Decimal("10.00"))

        # net_base_price = 100.00 - 10.00 = 90.00
        self.assertEqual(plan.net_base_price, Decimal("90.00"))

        # tax_amount = 18% of 90.00 = 16.20
        self.assertEqual(plan.tax_amount, Decimal("16.20"))

        # total_price = 90.00 + 16.20 = 106.20
        self.assertEqual(plan.total_price, Decimal("106.20"))

    def test_list_plans_endpoint(self):
        """
        Test GET request to list plans.
        """
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Verify success envelope in base viewset response
        self.assertTrue(response.data["status"])
        self.assertEqual(len(response.data["data"]), 1)

        result_plan = response.data["data"][0]
        self.assertEqual(result_plan["name"], "Starter Plan")
        self.assertEqual(Decimal(result_plan["total_price"]), Decimal("106.20"))

    def test_retrieve_plan_endpoint(self):
        """
        Test GET request to retrieve plan by ID.
        """
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertEqual(response.data["data"]["name"], "Starter Plan")

    def test_create_plan_endpoint(self):
        """
        Test POST request to create a plan.
        """
        payload = {
            "code": "enterprise_plan",
            "name": "Enterprise Plan",
            "description": "Unlimited access",
            "base_price": "500.00",
            "discount_percentage": "5.00",
            "tax_percentage": "12.00",
            "currency": "INR",
            "billing_cycle": "yearly",
            "plan_type": "paid",
            "sort_order": 2,
            "is_active": True
        }
        response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["status"])

        # Check DB creation and properties
        created_plan = Plan.objects.get(name="Enterprise Plan")
        self.assertEqual(created_plan.currency, Plan.CurrencyChoices.INR)
        # base = 500, discount = 5% (25) -> net = 475. tax = 12% (57) -> total = 532.00
        self.assertEqual(created_plan.total_price, Decimal("532.00"))

    def test_soft_delete_plan_endpoint(self):
        """
        Test DELETE request soft-deletes a plan.
        """
        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])

        # Soft deletion: model remains in DB with is_deleted=True, but excluded from standard querysets
        self.assertTrue(Plan.objects.filter(id=self.default_plan.id).exists() is False)
        # Querying direct model manager should show it
        self.assertTrue(Plan.all_objects.filter(id=self.default_plan.id).exists())

    def test_dropdown_endpoint(self):
        """
        Test GET request to dropdown returns value/label structure.
        """
        response = self.client.get(self.dropdown_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertEqual(len(response.data["data"]), 1)

        dropdown_item = response.data["data"][0]
        self.assertEqual(dropdown_item["value"], str(self.default_plan.id))
        # Expected label matches: "Starter Plan (Paid - 106.20 INR/Monthly)"
        self.assertEqual(dropdown_item["label"], "Starter Plan (Paid - 106.20 INR/Monthly)")

    def test_unauthenticated_access_endpoints(self):
        """
        Test that unauthenticated clients can access list, retrieve, and dropdown,
        but are blocked from mutating plans.
        """
        # De-authenticate the client
        self.client.force_authenticate(user=None)

        # 1. Test unauthenticated LIST
        list_response = self.client.get(self.list_url)
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)

        # 2. Test unauthenticated RETRIEVE
        detail_response = self.client.get(self.detail_url)
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)

        # 3. Test unauthenticated DROPDOWN
        dropdown_response = self.client.get(self.dropdown_url)
        self.assertEqual(dropdown_response.status_code, status.HTTP_200_OK)

        # 4. Test unauthenticated MUTATION (create is blocked)
        payload = {
            "code": "unauth_plan",
            "name": "Unauth Plan",
            "base_price": "100.00"
        }
        create_response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_standard_user_blocked_mutating(self):
        """
        Test that authenticated non-superuser standard clients are blocked from mutating plans (403 Forbidden).
        """
        # Authenticate standard user
        self.client.force_authenticate(user=self.user)

        # 1. Test POST creation is blocked
        payload = {
            "code": "standard_plan",
            "name": "Standard Plan Blocked",
            "base_price": "100.00"
        }
        create_response = self.client.post(self.list_url, data=payload, format="json")
        self.assertEqual(create_response.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Test DELETE is blocked
        delete_response = self.client.delete(self.detail_url)
        self.assertEqual(delete_response.status_code, status.HTTP_403_FORBIDDEN)
