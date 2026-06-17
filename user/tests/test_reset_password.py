from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient
from django.utils import timezone
from user.models import CustomUser
import random

class ActiveUserPasswordResetTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.forgot_url = reverse('api-v1:user-forgot-password')
        self.verify_url = reverse('api-v1:user-forgot-password-verify')
        self.reset_url = reverse('api-v1:user-reset-password')
        self.user = CustomUser.objects.create_user(
            email='alice@example.com',
            password='InitialPass123!'
        )
        self.user.is_active = True
        self.user.save()

    def test_forgot_and_reset_password_flow(self):
        """Complete OTP workflow for an active onboarding user using email-based identifier"""
        # Step 1: request OTP
        response = self.client.post(self.forgot_url, {"email": self.user.email}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Ensure no user details are leaked in the response data
        self.assertIsNone(response.data['data'])

        # Grab OTP from DB
        self.user.refresh_from_db()
        otp = self.user.otp_code
        self.assertIsNotNone(otp)

        # Step 2a: verify with wrong OTP
        resp = self.client.post(self.verify_url, {"email": self.user.email, "otp_code": '000000'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(resp.data['status'])
        self.assertIn('INVALID_OTP', resp.data['errors'])

        # Step 2b: make OTP expired
        self.user.otp_created_at = timezone.now() - timezone.timedelta(minutes=3)
        self.user.save(update_fields=['otp_created_at'])
        resp = self.client.post(self.verify_url, {"email": self.user.email, "otp_code": otp}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('OTP_EXPIRED', resp.data['errors'])

        # Reset OTP timestamp to valid
        self.user.otp_code = otp
        self.user.otp_created_at = timezone.now()
        self.user.save(update_fields=['otp_code', 'otp_created_at'])

        # Step 2c: successful verification
        resp = self.client.post(self.verify_url, {"email": self.user.email, "otp_code": otp}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['status'])
        token = resp.data['data']['token']
        self.assertIsNotNone(token)
        # OTP should be cleared
        self.user.refresh_from_db()
        self.assertIsNone(self.user.otp_code)
        self.assertIsNone(self.user.otp_created_at)

        # Step 2d: verification again should fail (OTP cleared)
        resp = self.client.post(self.verify_url, {"email": self.user.email, "otp_code": otp}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        # Step 3: reset password using token
        new_password = 'NewPass456!'
        resp = self.client.post(self.reset_url, {
            "email": self.user.email,
            "token": token,
            "password": new_password,
            "confirm_password": new_password
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['status'])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))

        # Step 4: reuse token should fail
        resp = self.client.post(self.reset_url, {
            "email": self.user.email,
            "token": token,
            "password": 'AnotherPass789!',
            "confirm_password": 'AnotherPass789!'
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('TOKEN_ALREADY_USED', resp.data['errors'])

    def test_prevent_user_enumeration(self):
        """Verify that password reset endpoints do not leak user existence"""
        # Step 1: Request OTP for non-existent email should return same success response
        response = self.client.post(self.forgot_url, {"email": "nonexistent@example.com"}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['data'])
        self.assertIn("verification OTP code will be sent shortly", response.data['message'])

        # Step 2: Verification for non-existent email should return invalid OTP error (not User Not Found)
        resp = self.client.post(self.verify_url, {"email": "nonexistent@example.com", "otp_code": "123456"}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('INVALID_OTP', resp.data['errors'])

        # Step 3: Resend OTP for non-existent email should return same success response
        resend_url = reverse('api-v1:user-forgot-password-resend')
        resp = self.client.post(resend_url, {"email": "nonexistent@example.com"}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsNone(resp.data['data'])

    def test_resend_forgot_password_otp(self):
        resend_url = reverse('api-v1:user-forgot-password-resend')
        
        # Step 1: Request OTP
        response = self.client.post(self.forgot_url, {"email": self.user.email}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Step 2: Attempt to resend OTP immediately (should fail due to 30-second cooldown)
        resp = self.client.post(resend_url, {"email": self.user.email}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('OTP_COOLDOWN_ACTIVE', resp.data['errors'])
        
        # Step 3: Advance time/timestamp back by 31 seconds
        self.user.refresh_from_db()
        self.user.otp_created_at = timezone.now() - timezone.timedelta(seconds=31)
        self.user.save()
        
        # Step 4: Resend OTP (should succeed)
        resp = self.client.post(resend_url, {"email": self.user.email}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['status'])
        
        # Grab new OTP and verify it has changed (or at least been refreshed)
        self.user.refresh_from_db()
        new_otp = self.user.otp_code
        self.assertIsNotNone(new_otp)

    def test_login_lockout_enforced_by_defender(self):
        """Verify custom DRF LoginAPIView lockout when already locked in defender"""
        from defender.utils import block_username, unblock_username

        login_url = reverse('api-v1:login')
        
        # Manually lock user in defender cache
        block_username(self.user.email)
        
        try:
            # Attempt to login using APIClient - should get 403 Forbidden instantly
            resp = self.client.post(login_url, {
                "email": self.user.email,
                "password": "InitialPass123!"
            }, format='json')
            
            self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
            self.assertFalse(resp.data['status'])
            self.assertIn("Account or IP temporarily locked", resp.data['message'])
        finally:
            # Clean up cache
            unblock_username(self.user.email)
