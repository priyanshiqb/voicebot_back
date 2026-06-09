from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient
from user.models.user_session import UserSession

User = get_user_model()

class UserSessionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.login_url = reverse("api-v1:login")
        self.refresh_url = reverse("api-v1:token_refresh")
        self.logout_url = reverse("api-v1:logout")
        self.sessions_list_url = reverse("api-v1:usersession-list")
        self.revoke_all_url = reverse("api-v1:usersession-revoke-all")

        # Create active user
        self.user = User.objects.create_user(
            email="test.session@example.com",
            password="SecurePass123!",
            first_name="Test",
            last_name="Session"
        )
        self.user.is_active = True
        self.user.save()

    def test_session_lifecycle(self):
        """
        Verify session creation on login, retrieval, rotation on refresh, and single logout.
        """
        # 1. Login to create a session
        payload = {
            "email": "test.session@example.com",
            "password": "SecurePass123!"
        }
        response = self.client.post(self.login_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        access_token = response.data["data"]["access"]
        refresh_token = response.data["data"]["refresh"]

        # Verify UserSession record was created
        self.assertEqual(UserSession.objects.filter(user=self.user, is_active=True).count(), 1)
        session = UserSession.objects.get(user=self.user, is_active=True)
        self.assertIsNotNone(session.access_jti)
        self.assertIsNotNone(session.refresh_jti)

        # 2. Access protected endpoint (List Sessions)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        response = self.client.get(self.sessions_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)
        self.assertTrue(response.data["data"][0]["is_current"])

        # 3. Token Refresh (Rotates token and updates UserSession)
        self.client.credentials()  # Clear auth credentials for refresh call
        refresh_payload = {"refresh": refresh_token}
        response = self.client.post(self.refresh_url, data=refresh_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        new_access = response.data["data"]["access"]
        new_refresh = response.data["data"]["refresh"]

        # Verify JTI updated in session
        session.refresh_from_db()
        self.assertNotEqual(session.access_jti, None)
        
        # 4. Old Access Token is rejected after rotation/refresh
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        response = self.client.get(self.sessions_list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # New Access Token is valid
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {new_access}")
        response = self.client.get(self.sessions_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # 5. Revoke Single Session (Sign out current device)
        revoke_url = reverse("api-v1:usersession-detail", kwargs={"pk": session.id})
        response = self.client.delete(revoke_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Session is inactive in DB
        session.refresh_from_db()
        self.assertFalse(session.is_active)

        # Access token is now rejected
        response = self.client.get(self.sessions_list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_revoke_all_other_sessions(self):
        """
        Verify that revoking all other sessions signs out other devices but keeps the current one active.
        """
        # Create first session (simulating device 1)
        login_payload = {
            "email": "test.session@example.com",
            "password": "SecurePass123!"
        }
        res1 = self.client.post(self.login_url, data=login_payload, format="json")
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        access1 = res1.data["data"]["access"]

        # Create second session (simulating device 2)
        res2 = self.client.post(self.login_url, data=login_payload, format="json")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        access2 = res2.data["data"]["access"]

        # Total active sessions should be 2
        self.assertEqual(UserSession.objects.filter(user=self.user, is_active=True).count(), 2)

        # From Device 2, call revoke-all (to sign out Device 1)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access2}")
        response = self.client.post(self.revoke_all_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Device 1 access token is now invalid/rejected
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access1}")
        response = self.client.get(self.sessions_list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        # Device 2 access token remains active/valid
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access2}")
        response = self.client.get(self.sessions_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["data"]), 1)  # Only 1 active session remains

    def test_session_last_used_updates(self):
        """
        Verify that the last_used field is updated when a user interacts with the API,
        respecting the throttled cooldown period.
        """
        import datetime
        from django.utils import timezone
        from django.core.cache import cache

        # 1. Login to get a session
        payload = {
            "email": "test.session@example.com",
            "password": "SecurePass123!"
        }
        response = self.client.post(self.login_url, data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        access_token = response.data["data"]["access"]
        
        session = UserSession.objects.get(user=self.user, is_active=True)
        initial_last_used = session.last_used
        self.assertIsNotNone(initial_last_used)
        
        # 2. Simulate time passing and bypass the 60-second throttling
        session.last_used = timezone.now() - datetime.timedelta(seconds=70)
        session.save(update_fields=["last_used"])
        
        # Clear cache to force DB reload
        cache.clear()
        
        # 3. Access protected API
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        response = self.client.get(self.sessions_list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # 4. Check if last_used has updated
        session.refresh_from_db()
        self.assertGreater(session.last_used, initial_last_used)


