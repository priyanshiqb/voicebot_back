from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from onboard.models.plan import Plan
from user.models import CustomUser
from decimal import Decimal


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
            "phone_number": "919876543211",
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
        self.assertIn("access", response.data["data"])
        self.assertIn("refresh", response.data["data"])

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

    def test_change_email_uniqueness_validation(self):
        """
        Verify that changing email to an already registered active or pending email
        is gracefully validated and rejected with a 400 error instead of a 500 constraint error.
        """
        # 1. Register first pending user (temp_user_1)
        signup_payload_1 = {
            "plan_id": str(self.plan.id),
            "first_name": "First",
            "last_name": "User",
            "business_name": "First Inc",
            "email": "first@example.com",
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!"
        }
        res1 = self.client.post(self.register_url, data=signup_payload_1, format="json")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

        # 2. Register second pending user (temp_user_2)
        signup_payload_2 = {
            "plan_id": str(self.plan.id),
            "first_name": "Second",
            "last_name": "User",
            "business_name": "Second Inc",
            "email": "second@example.com",
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!"
        }
        res2 = self.client.post(self.register_url, data=signup_payload_2, format="json")
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        temp_id_2 = res2.data["data"]["temp_registration_id"]

        # 3. Try to change second user's email to the first user's email (pending conflict)
        change_email_url = reverse("api-v1:change-email")
        change_payload_pending = {
            "temp_registration_id": temp_id_2,
            "new_email": "first@example.com"
        }
        change_res_pending = self.client.post(change_email_url, data=change_payload_pending, format="json")
        self.assertEqual(change_res_pending.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(change_res_pending.data["status"])
        self.assertIn("new_email", change_res_pending.data["errors"])

        # 4. Create an active CustomUser
        CustomUser.objects.create_user(
            email="active@example.com",
            password="SecurePassword123!",
            first_name="Active",
            last_name="User"
        )

        # 5. Try to change second user's email to the active user's email (active conflict)
        change_payload_active = {
            "temp_registration_id": temp_id_2,
            "new_email": "active@example.com"
        }
        change_res_active = self.client.post(change_email_url, data=change_payload_active, format="json")
        self.assertEqual(change_res_active.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(change_res_active.data["status"])
        self.assertIn("new_email", change_res_active.data["errors"])

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
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!"
        }
        response = self.client.post(self.register_url, data=signup_payload, format="json")
        temp_id = response.data["data"]["temp_registration_id"]

        from onboard.models import TempUser
        temp_user = TempUser.objects.get(id=temp_id)
        
        # Step 2: POST resume-session (when email is NOT verified yet)
        resume_url = reverse("api-v1:resume-session")
        resume_payload = {"email": "resume.user@example.com", "password": "SecurePassword123!"}
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

        # Step 4: POST resume-session with incorrect password (should fail)
        invalid_payload = {"email": "resume.user@example.com", "password": "WrongPassword!"}
        response = self.client.post(resume_url, data=invalid_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["errors"], ["INVALID_CREDENTIALS"])

        # Step 5: POST resume-session with missing password (should fail)
        missing_password_payload = {"email": "resume.user@example.com"}
        response = self.client.post(resume_url, data=missing_password_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data["errors"])

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

    def test_signup_no_phone_number_and_referral_code_success(self):
        """
        Verify that registering without a phone number is successful,
        and entering a referral code successfully tracks the referral
        code across the temporary to custom user promotion lifecycle.
        """
        # Step 1: POST register-pending with no phone_number and a referral_code
        signup_payload = {
            "plan_id": str(self.plan.id),
            "first_name": "John",
            "last_name": "Smith",
            "business_name": "Smith Corp",
            "email": "john.smith@example.com",
            "referral_code": "REF12345",
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!"
        }
        response = self.client.post(self.register_url, data=signup_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["status"])
        temp_id = response.data["data"]["temp_registration_id"]

        from onboard.models import TempUser
        temp_user = TempUser.objects.get(id=temp_id)
        self.assertFalse(temp_user.is_email_verified)
        self.assertEqual(temp_user.referral_code, "REF12345")
        self.assertIsNone(temp_user.phone_number)

        # Step 2: POST verify-otp
        otp_payload = {
            "temp_registration_id": temp_id,
            "otp_code": temp_user.otp_code
        }
        response = self.client.post(self.verify_otp_url, data=otp_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        temp_token = response.data["data"]["temporary_token"]

        # Step 3: POST create-order-just-in-time
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {temp_token}")
        address_payload = {
            "phone_number": "919876543211",
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
        order_id = response.data["data"]["razorpay_order_id"]

        # Step 4: POST verify to activate the user
        verify_payload = {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": "pay_mockpaymentid99",
            "razorpay_signature": "mocksignaturehash99"
        }
        response = self.client.post(self.verify_payment_url, data=verify_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify that CustomUser was promoted and has captured phone_number
        user = CustomUser.objects.get(email="john.smith@example.com")
        self.assertEqual(user.phone_number, "919876543211")

        # Verify that a Tenant organization was successfully created and mapped
        from tenant.models import Tenant, TenantUserMapping
        self.assertEqual(Tenant.objects.count(), 1)
        tenant = Tenant.objects.first()
        self.assertEqual(tenant.name, "Smith Corp")
        self.assertEqual(tenant.subdomain, "smith-corp")
        self.assertEqual(tenant.referred_by_code, "REF12345")
        self.assertTrue(tenant.tenant_referral_code.startswith("REF-"))
        self.assertEqual(len(tenant.tenant_referral_code), 12) # "REF-" + 8 chars = 12

        self.assertEqual(TenantUserMapping.objects.count(), 1)
        mapping = TenantUserMapping.objects.first()
        self.assertEqual(mapping.user, user)
        self.assertEqual(mapping.tenant, tenant)

    def test_change_email_during_otp_success(self):
        """
        Verify that a user can change their email address during the OTP stage,
        which updates the TempUser record and sends a new OTP code to the new email.
        """
        # Step 1: POST register-pending
        signup_payload = {
            "plan_id": str(self.plan.id),
            "first_name": "Alice",
            "last_name": "Wonder",
            "business_name": "Wonderland",
            "email": "alice.old@example.com",
            "password": "SecurePassword123!",
            "confirm_password": "SecurePassword123!"
        }
        response = self.client.post(self.register_url, data=signup_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        temp_id = response.data["data"]["temp_registration_id"]

        from onboard.models import TempUser
        temp_user = TempUser.objects.get(id=temp_id)
        old_otp = temp_user.otp_code

        # Step 2: POST change-email
        change_email_url = "/api/v1/onboard/change-email/"
        change_payload = {
            "temp_registration_id": temp_id,
            "new_email": "alice.new@example.com"
        }
        response = self.client.post(change_email_url, data=change_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertEqual(response.data["data"]["email"], "alice.new@example.com")

        # Verify that TempUser details are updated in the database
        temp_user.refresh_from_db()
        self.assertEqual(temp_user.email, "alice.new@example.com")
        self.assertNotEqual(temp_user.otp_code, old_otp)

        # Step 3: POST verify-otp with the new email/new OTP
        otp_payload = {
            "temp_registration_id": temp_id,
            "otp_code": temp_user.otp_code
        }
        response = self.client.post(self.verify_otp_url, data=otp_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])

    def test_forgot_and_reset_password_temp_user_flow(self):
        """
        Verify that unverified TempUsers can successfully request a password reset,
        receive an OTP verification code, verify the OTP, receive a signed token,
        use that token to update password, and verify OTP/token invalidation scopes.
        """
        # Step 1: POST register-pending
        signup_payload = {
            "plan_id": str(self.plan.id),
            "first_name": "Bob",
            "last_name": "Builder",
            "business_name": "Bob Builds",
            "email": "bob.builder@example.com",
            "password": "InitialSecurePassword123!",
            "confirm_password": "InitialSecurePassword123!"
        }
        response = self.client.post(self.register_url, data=signup_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        temp_id = response.data["data"]["temp_registration_id"]

        from onboard.models import TempUser
        temp_user = TempUser.objects.get(id=temp_id)
        original_hash = temp_user.password_hash

        # Step 2: POST forgot-password
        forgot_url = "/api/v1/onboard/forgot-password/"
        forgot_payload = {"email": "bob.builder@example.com"}
        response = self.client.post(forgot_url, data=forgot_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["temp_registration_id"], temp_id)

        # Retrieve OTP from database
        temp_user.refresh_from_db()
        otp = temp_user.otp_code
        self.assertIsNotNone(otp)

        # Step 2.5: POST forgot-password/verify/ (Fail with invalid OTP)
        verify_url = "/api/v1/onboard/forgot-password/verify/"
        verify_payload_invalid = {
            "temp_registration_id": temp_id,
            "otp_code": "000000"
        }
        response = self.client.post(verify_url, data=verify_payload_invalid, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["status"])
        self.assertIn("INVALID_OTP", response.data["errors"])

        # Step 2.6: POST forgot-password/verify/ (Fail with expired OTP)
        from django.utils import timezone
        temp_user.otp_created_at = timezone.now() - timezone.timedelta(minutes=3)
        temp_user.save()
        verify_payload_good = {
            "temp_registration_id": temp_id,
            "otp_code": otp
        }
        response = self.client.post(verify_url, data=verify_payload_good, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("OTP_EXPIRED", response.data["errors"])

        # Reset OTP and set valid time
        temp_user.otp_code = otp
        temp_user.otp_created_at = timezone.now()
        temp_user.save()

        # Step 2.7: POST forgot-password/verify/ (Success)
        response = self.client.post(verify_url, data=verify_payload_good, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        token = response.data["data"]["token"]
        self.assertIsNotNone(token)

        # Verify OTP is invalidated in DB
        temp_user.refresh_from_db()
        self.assertIsNone(temp_user.otp_code)

        # Step 2.8: POST forgot-password/verify/ again (Fail since already invalidated)
        response = self.client.post(verify_url, data=verify_payload_good, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Step 3: POST reset-password (Success)
        reset_url = "/api/v1/onboard/reset-password/"
        reset_payload = {
            "temp_registration_id": temp_id,
            "token": token,
            "password": "NewSecurePassword999!",
            "confirm_password": "NewSecurePassword999!"
        }
        response = self.client.post(reset_url, data=reset_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])

        # Check database password_hash is updated
        temp_user.refresh_from_db()
        self.assertNotEqual(temp_user.password_hash, original_hash)

        # Step 4: POST reset-password again with same token (Should Fail due to one-time-use constraint)
        response = self.client.post(reset_url, data=reset_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["status"])
        self.assertIn("TOKEN_ALREADY_USED", response.data["errors"])

    def test_check_email_availability(self):
        """
        Verify that the check-email endpoint correctly and quickly reports email availability
        status by querying both active CustomUser and pending TempUser databases.
        """
        check_email_url = reverse("api-v1:check-email")

        # Scenario 1: Email is fully available
        payload = {"email": "fully.available@example.com"}
        response = self.client.post(check_email_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
        self.assertTrue(response.data["data"]["available"])
        self.assertFalse(response.data["data"]["exists_in_active_users"])
        self.assertFalse(response.data["data"]["exists_in_pending_registrations"])

        # Scenario 2: Email exists in active CustomUser database
        CustomUser.objects.create_user(
            email="testuser@example.com",
            password="testpassword123",
            first_name="Test",
            last_name="User"
        )
        payload = {"email": "testuser@example.com"}
        response = self.client.post(check_email_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["data"]["available"])
        self.assertTrue(response.data["data"]["exists_in_active_users"])
        self.assertFalse(response.data["data"]["exists_in_pending_registrations"])

        # Scenario 3: Email exists in pending TempUser database
        # Create a pending TempUser first
        from onboard.models import TempUser
        TempUser.objects.create(
            email="pending.registrant@example.com",
            first_name="Jane",
            last_name="Doe",
            business_name="Jane Tech",
            password_hash="somehash"
        )
        payload = {"email": "pending.registrant@example.com"}
        response = self.client.post(check_email_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["data"]["available"])
        self.assertFalse(response.data["data"]["exists_in_active_users"])
        self.assertTrue(response.data["data"]["exists_in_pending_registrations"])

    def test_onboarding_forgot_password_resend_otp(self):
        from onboard.models import TempUser
        from django.utils import timezone
        
        # 1. Create a pending TempUser
        temp_user = TempUser.objects.create(
            email="forgot.temp@example.com",
            first_name="Forgot",
            last_name="Temp",
            business_name="Forgot Temp Ltd",
            password_hash="somehash"
        )
        
        forgot_url = reverse("api-v1:forgot-password")
        resend_url = reverse("api-v1:forgot-password-resend")
        
        # 2. Trigger forgot password OTP
        response = self.client.post(forgot_url, {"email": temp_user.email}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        temp_id = response.data["data"]["temp_registration_id"]
        
        # 3. Resend OTP immediately (should fail due to 30s cooldown)
        response = self.client.post(resend_url, {"temp_registration_id": temp_id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("OTP_COOLDOWN_ACTIVE", response.data["errors"])
        
        # 4. Advance cooldown time
        temp_user.refresh_from_db()
        temp_user.otp_created_at = timezone.now() - timezone.timedelta(seconds=31)
        temp_user.save()
        
        # 5. Resend OTP (should succeed)
        response = self.client.post(resend_url, {"temp_registration_id": temp_id}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["status"])
