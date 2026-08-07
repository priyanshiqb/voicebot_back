"""
Clearing the SMTP test-send rate limit from the admin.

The thing most worth testing is not the deletion — it is that the key we delete is
the key DRF wrote. Those are computed in two different places, and if they drift
the action reports success while the caller stays blocked, which is worse than no
action at all. So these tests throttle a real request first and assert the block
actually lifts, rather than asserting that some key disappeared.
"""

from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.test import RequestFactory, override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from tenant.admin.TenantAdmin import TenantAdmin
from tenant.models.Tenant import Tenant
from tenant.models.TenantEmailConfiguration import TenantEmailConfiguration
from tenant.models.TenantUserMapping import TenantUserMapping
from tenant.views.tenant_email_configuration import SMTPTestRateThrottle
from user.admin.custom_user import CustomUserAdmin
from user.models import CustomUser as User
from utils.mail import connection as mail_connection
from utils.throttling import (
    SMTP_TEST_SCOPE,
    clear_smtp_test_throttles,
    throttle_cache_key,
)

URL = "/api/v1/tenant/email-configurations/"


class ScopeMatchesTheThrottleTests(APITestCase):
    def test_the_scope_constant_matches_the_throttle_it_clears(self):
        """`SMTP_TEST_SCOPE` is a copy of the throttle's scope, kept separate to
        avoid an import cycle. If they drift, every clear silently deletes a key
        that was never written and the action reports "nothing to clear"."""
        self.assertEqual(SMTP_TEST_SCOPE, SMTPTestRateThrottle.scope)


@override_settings(SMTP_TEST_THROTTLE_PER_HOUR=2)
class ClearingUnblocksTheCallerTests(APITestCase):
    """End-to-end: throttle the real endpoint, clear, confirm it lets you back in."""

    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-throttle")
        self.user = User.objects.create_user(
            username="throttled", email="throttled@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)

        self.config = TenantEmailConfiguration.objects.create(
            tenant=self.tenant,
            name="Primary",
            provider=TenantEmailConfiguration.Provider.CUSTOM,
            host="smtp.example.invalid",
            port=587,
            from_email="bookings@example.com",
        )
        self.client_alex = APIClient()
        self.client_alex.force_authenticate(user=self.user)
        self.test_url = f"{URL}{self.config.id}/test/"

    def _exhaust(self):
        with patch.object(mail_connection, "send_test_email", return_value=(True, "")):
            for _ in range(2):
                self.client_alex.post(self.test_url)
            return self.client_alex.post(self.test_url).status_code

    def test_clearing_lets_a_throttled_user_back_in(self):
        self.assertEqual(self._exhaust(), status.HTTP_429_TOO_MANY_REQUESTS)

        self.assertEqual(clear_smtp_test_throttles([self.user.pk]), 1)

        with patch.object(mail_connection, "send_test_email", return_value=(True, "")):
            response = self.client_alex.post(self.test_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_clearing_an_unthrottled_user_reports_nothing_cleared(self):
        """"Nothing was throttled" and "you are unblocked" must not look alike —
        the first means the 429 being reported comes from somewhere else."""
        self.assertEqual(clear_smtp_test_throttles([self.user.pk]), 0)

    def test_one_users_reset_does_not_lift_anothers(self):
        other = User.objects.create_user(
            username="other", email="other@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=other, tenant=self.tenant)
        self.assertEqual(self._exhaust(), status.HTTP_429_TOO_MANY_REQUESTS)

        clear_smtp_test_throttles([other.pk])

        with patch.object(mail_connection, "send_test_email", return_value=(True, "")):
            response = self.client_alex.post(self.test_url)
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


@override_settings(SMTP_TEST_THROTTLE_PER_HOUR=2)
class AdminActionTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-action")
        self.other_tenant = Tenant.objects.create(
            name="Globex", subdomain="globex-action"
        )

        self.superuser = User.objects.create_superuser(
            email="root@example.com", password="password", username="root-action"
        )
        self.member = User.objects.create_user(
            username="member", email="member@example.com", password="password"
        )
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.member, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.outsider, tenant=self.other_tenant)

    def _throttle(self, user):
        """Write a throttle history the way DRF would, for `user`."""
        cache.set(throttle_cache_key(SMTP_TEST_SCOPE, user.pk), [1.0, 2.0], 3600)

    def _is_throttled(self, user):
        return cache.get(throttle_cache_key(SMTP_TEST_SCOPE, user.pk)) is not None

    def _request(self, user):
        request = self.factory.post("/admin/")
        request.user = user
        # message_user writes to the messages framework, which needs a store.
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def test_the_user_action_clears_only_the_selected_users(self):
        self._throttle(self.member)
        self._throttle(self.outsider)

        model_admin = CustomUserAdmin(User, django_admin.site)
        model_admin.clear_smtp_test_rate_limit(
            self._request(self.superuser), User.objects.filter(pk=self.member.pk)
        )

        self.assertFalse(self._is_throttled(self.member))
        self.assertTrue(self._is_throttled(self.outsider))

    def test_the_tenant_action_clears_every_member_and_nobody_else(self):
        self._throttle(self.member)
        self._throttle(self.outsider)

        model_admin = TenantAdmin(Tenant, django_admin.site)
        model_admin.clear_smtp_test_rate_limit(
            self._request(self.superuser), Tenant.objects.filter(pk=self.tenant.pk)
        )

        self.assertFalse(self._is_throttled(self.member))
        # The other tenant's user is untouched — this is a tenant-scoped action.
        self.assertTrue(self._is_throttled(self.outsider))

    def test_the_actions_are_hidden_from_non_superusers(self):
        """Someone who can reset their own limit at will is not throttled."""
        staff = User.objects.create_user(
            username="staff-action",
            email="staff-action@example.com",
            password="password",
            is_staff=True,
        )

        for model_admin in (
            CustomUserAdmin(User, django_admin.site),
            TenantAdmin(Tenant, django_admin.site),
        ):
            with self.subTest(admin=type(model_admin).__name__):
                self.assertIn(
                    "clear_smtp_test_rate_limit",
                    model_admin.get_actions(self._request(self.superuser)),
                )
                self.assertNotIn(
                    "clear_smtp_test_rate_limit",
                    model_admin.get_actions(self._request(staff)),
                )
