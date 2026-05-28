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


class OnboardingTests(APITestCase):
    """
    Complete integration test suite for the dynamic OTP-based user signup funnel
    and compliant just-in-time Razorpay order creation.
    """

    def setUp(self):
        self.plan = Plan.objects.create(
            code="starter_inr",
            name="Starter Plan",
            description="Perfect for startups",
            base_price=Decimal("2000.00"),
            discount_percentage=Decimal("10.00"),
            tax_percentage=Decimal("18.00"),
            currency=Plan.CurrencyChoices.INR,
            billing_cycle=Plan.BillingCycleChoices.MONTHLY,
            plan_type=Plan.PlanTypeChoices.PAID,
            sort_order=1,
            is_active=True
        )

        self.pro_plan = Plan.objects.create(
            code="pro_inr",
            name="Pro Plan",
            description="Perfect for enterprises",
            base_price=Decimal("4000.00"),
            discount_percentage=Decimal("0.00"),
            tax_percentage=Decimal("18.00"),
            currency=Plan.CurrencyChoices.INR,
            billing_cycle=Plan.BillingCycleChoices.MONTHLY,
            plan_type=Plan.PlanTypeChoices.PAID,
            sort_order=2,
            is_active=True
        )

        self.register_url = reverse("api-v1:register-pending")
        self.verify_otp_url = reverse("api-v1:verify-otp")
        self.update_plan_url = reverse("api-v1:update-plan")
        self.create_order_url = reverse("api-v1:create-order-just-in-time")
        self.verify_payment_url = reverse("api-v1:verify")
        self.cancel_order_url = reverse("api-v1:cancel-order")

    def test_signup_funnel_success(self):
        """
        Verify the full end-to-end user signup and payment activation funnel.
        """
        # Step 1: POST register-pending
        signup_payload = {
            "plan_id": str(self.plan.id),
            "first_name": "Jane",
            "last_name": "Doe",
            "business_name": "Doe Enterprises",
            "email": "jane.doe@example.com",
            "phone_number": "919876543211",
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!"
        }
        response = self.client.post(self.register_url, data=signup_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["status"])
        temp_id = response.data["data"]["temp_registration_id"]

        from onboard.models import TempUser
        temp_user = TempUser.objects.get(id=temp_id)
        self.assertEqual(temp_user.is_email_verified, False)
        self.assertIsNotNone(temp_user.otp_code)

        # Step 2: POST verify-otp
        otp_payload = {
            "temp_registration_id": temp_id,
            "otp_code": temp_user.otp_code
        }
        response = self.client.post(self.verify_otp_url, data=otp_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        temp_token = response.data["data"]["temporary_token"]

        # Verify pricing breakdown returned on OTP verify matches plan properties
        # starter_inr: base = 2000, discount = 10% (200) -> net = 1800. tax = 18% (324) -> total = 2124.00
        self.assertEqual(response.data["data"]["billing_breakdown"]["total_price"], "2124.00")

        # Step 3: PATCH update-plan (Dynamically upgrade to Pro Plan)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {temp_token}")
        update_payload = {
            "plan_id": str(self.pro_plan.id)
        }
        response = self.client.patch(self.update_plan_url, data=update_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        # Pro Plan: base = 4000, discount = 0 -> net = 4000. tax = 18% (720) -> total = 4720.00
        self.assertEqual(response.data["data"]["billing_breakdown"]["total_price"], "4720.00")

        # Step 4: POST create-order-just-in-time
        address_payload = {
            "billing_address": {
                "line1": "123 Tech Square",
                "city": "Bengaluru",
                "state": "Karnataka",
                "zip_code": "560001",
                "country": "IN",
                "gstin": "29AAAAA1111A1Z1"
            }
        }
        response = self.client.post(self.create_order_url, data=address_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        order_id = response.data["data"]["razorpay_order_id"]
        # Amount in Paisa: 4720.00 * 100 = 472000
        self.assertEqual(response.data["data"]["amount"], 472000)

        # Step 5: POST verify
        verify_payload = {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_dummy_12345",
            "razorpay_signature": "signature_dummy_hash"
        }
        response = self.client.post(self.verify_payment_url, data=verify_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertIn("access", response.data["data"]["tokens"])

        # Assert TempUser is promoted and removed
        self.assertFalse(TempUser.objects.filter(id=temp_id).exists())
        self.assertTrue(CustomUser.objects.filter(email="jane.doe@example.com").exists())

    def test_signup_validation_mismatched_passwords(self):
        """
        Verify that registration triggers validation errors for mismatched passwords.
        """
        payload = {
            "plan_id": str(self.plan.id),
            "first_name": "John",
            "last_name": "Smith",
            "business_name": "Smith Corp",
            "email": "john.smith@example.com",
            "phone_number": "919876543212",
            "password": "SecurePassword123!",
            "confirm_password": "DifferentPassword!"
        }
        response = self.client.post(self.register_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["status"])
        self.assertIn("confirm_password", response.data["errors"])

    def test_invalid_otp_fails(self):
        """
        Verify that verifying with a bad OTP code triggers a 400 validation error.
        """
        signup_payload = {
            "plan_id": str(self.plan.id),
            "first_name": "John",
            "last_name": "Baker",
            "business_name": "Baker Hub",
            "email": "john.baker@example.com",
            "phone_number": "919876543213",
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!"
        }
        response = self.client.post(self.register_url, data=signup_payload, format="json")
        temp_id = response.data["data"]["temp_registration_id"]

        otp_payload = {
            "temp_registration_id": temp_id,
            "otp_code": "00000"  # Invalid 5-digit OTP
        }
        response = self.client.post(self.verify_otp_url, data=otp_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["status"])

    def test_resend_otp_cooldown_and_limit(self):
        """
        Verify that resending OTP inside 30 seconds cooldown is blocked, and works after.
        """
        signup_payload = {
            "plan_id": str(self.plan.id),
            "first_name": "Resend",
            "last_name": "User",
            "business_name": "Resend Inc",
            "email": "resend.user@example.com",
            "phone_number": "919876543214",
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!"
        }
        response = self.client.post(self.register_url, data=signup_payload, format="json")
        temp_id = response.data["data"]["temp_registration_id"]

        # Call Resend OTP immediately (should be blocked by 30 seconds cooldown)
        resend_url = reverse("api-v1:resend-otp")
        resend_payload = {"temp_registration_id": temp_id}
        response = self.client.post(resend_url, data=resend_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["errors"], ["OTP_COOLDOWN_ACTIVE"])

        # Mock-advance otp_created_at in database to bypass cooldown
        from onboard.models import TempUser
        from django.utils import timezone
        import datetime
        temp_user = TempUser.objects.get(id=temp_id)
        temp_user.otp_created_at = timezone.now() - datetime.timedelta(seconds=45)
        temp_user.save()

        # Resend OTP again (should now succeed after 30 seconds elapsed)
        response = self.client.post(resend_url, data=resend_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])

    def test_resume_session_state_promotion(self):
        """
        Verify resuming session reissues a fresh temp token and restores state where they left off.
        """
        # Step 1: POST register-pending
        signup_payload = {
            "plan_id": str(self.plan.id),
            "first_name": "Resume",
            "last_name": "User",
            "business_name": "Resume Inc",
            "email": "resume.user@example.com",
            "phone_number": "919876543215",
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!"
        }
        response = self.client.post(self.register_url, data=signup_payload, format="json")
        temp_id = response.data["data"]["temp_registration_id"]

        from onboard.models import TempUser
        temp_user = TempUser.objects.get(id=temp_id)
        
        # Step 2: POST resume-session (when email is NOT verified yet)
        resume_url = reverse("api-v1:resume-session")
        resume_payload = {"email": "resume.user@example.com"}
        response = self.client.post(resume_url, data=resume_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["step"], "OTP_VERIFICATION_REQUIRED")

        # Verify Email and test verified state resume
        temp_user.is_email_verified = True
        temp_user.save()

        # Step 3: POST resume-session (when email IS verified)
        response = self.client.post(resume_url, data=resume_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(response.data["data"]["temporary_token"])
        self.assertEqual(response.data["data"]["step"], "BILLING_ADDRESS_REQUIRED")

    def test_strict_gstin_state_validation(self):
        """
        Verify that only officially valid Indian state code prefixes are accepted for GSTIN.
        """
        from onboard.serializers.onboarding import BillingAddressSerializer
        
        # 1. Invalid prefix "00" (State prefix codes must be between 01 and 38, or 97-99)
        payload_invalid_prefix = {
            "line1": "Flat 304",
            "city": "Bengaluru",
            "state": "Karnataka",
            "zip_code": "560001",
            "country": "IN",
            "gstin": "00AAAAA1111A1Z1"
        }
        serializer = BillingAddressSerializer(data=payload_invalid_prefix)
        self.assertFalse(serializer.is_valid())
        self.assertIn("gstin", serializer.errors)

        # 2. Valid prefix "29" (Karnataka state prefix)
        payload_valid_prefix = {
            "line1": "Flat 304",
            "city": "Bengaluru",
            "state": "Karnataka",
            "zip_code": "560001",
            "country": "IN",
            "gstin": "29AAAAA1111A1Z1"
        }
        serializer = BillingAddressSerializer(data=payload_valid_prefix)
        self.assertTrue(serializer.is_valid())

    def test_webhook_historical_auditing(self):
        """
        Verify that every incoming Webhook post generates an immutable audit record
        in our RazorpayWebhookLog database table.
        """
        from onboard.models import RazorpayWebhookLog
        
        # Verify database is currently empty of webhook logs
        self.assertEqual(RazorpayWebhookLog.objects.count(), 0)

        # Trigger mock webhook POST payload
        webhook_payload = {
            "id": "evt_test_123456",
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_test_payment_99",
                        "order_id": "order_test_dummy",
                        "amount": 472000,
                        "status": "captured"
                    }
                }
            }
        }
        webhook_url = reverse("api-v1:webhook")
        
        # Set custom headers
        self.client.credentials(HTTP_X_RAZORPAY_SIGNATURE="mock_signature_checksum")
        response = self.client.post(webhook_url, data=webhook_payload, format="json")
        
        # Assert response is success (since we are testing in mock context)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify that an audit record was written to the database!
        self.assertEqual(RazorpayWebhookLog.objects.count(), 1)
        log = RazorpayWebhookLog.objects.first()
        self.assertEqual(log.event_id, "evt_test_123456")
        self.assertEqual(log.event_type, "payment.captured")
        self.assertEqual(log.signature, "mock_signature_checksum")
        self.assertTrue(log.is_signature_valid)
        self.assertEqual(log.payload["id"], "evt_test_123456")




