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
        """Complete OTP workflow for an active onboarding user"""
        # Step 1: request OTP
        response = self.client.post(self.forgot_url, {"email": self.user.email}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('user_id', response.data['data'])
        user_id = response.data['data']['user_id']
        self.assertEqual(user_id, str(self.user.id))
        # Grab OTP from DB
        self.user.refresh_from_db()
        otp = self.user.otp_code
        self.assertIsNotNone(otp)

        # Step 2a: verify with wrong OTP
        resp = self.client.post(self.verify_url, {"user_id": user_id, "otp_code": '000000'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(resp.data['status'])
        self.assertIn('INVALID_OTP', resp.data['errors'])

        # Step 2b: make OTP expired
        self.user.otp_created_at = timezone.now() - timezone.timedelta(minutes=3)
        self.user.save(update_fields=['otp_created_at'])
        resp = self.client.post(self.verify_url, {"user_id": user_id, "otp_code": otp}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('OTP_EXPIRED', resp.data['errors'])

        # Reset OTP timestamp to valid
        self.user.otp_code = otp
        self.user.otp_created_at = timezone.now()
        self.user.save(update_fields=['otp_code', 'otp_created_at'])

        # Step 2c: successful verification
        resp = self.client.post(self.verify_url, {"user_id": user_id, "otp_code": otp}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data['status'])
        token = resp.data['data']['token']
        self.assertIsNotNone(token)
        # OTP should be cleared
        self.user.refresh_from_db()
        self.assertIsNone(self.user.otp_code)
        self.assertIsNone(self.user.otp_created_at)

        # Step 2d: verification again should fail (OTP cleared)
        resp = self.client.post(self.verify_url, {"user_id": user_id, "otp_code": otp}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

        # Step 3: reset password using token
        new_password = 'NewPass456!'
        resp = self.client.post(self.reset_url, {
            "user_id": user_id,
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
            "user_id": user_id,
            "token": token,
            "password": 'AnotherPass789!',
            "confirm_password": 'AnotherPass789!'
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('TOKEN_ALREADY_USED', resp.data['errors'])

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
