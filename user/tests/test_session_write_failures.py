"""
What happens when the session row cannot be written.

Every authenticated request resolves its access token's ``jti`` against a
``UserSession`` row, so a token pair issued **without** one is not a degraded login —
it is a dead login. Every subsequent call 401s.

All three places that mint a pair used to wrap the write in
``except Exception: logger.warning(...)`` and return the tokens anyway. A transient
database failure therefore produced credentials that looked perfectly normal, worked
nowhere, and explained themselves only in a worker log nobody was watching. From the
user's side: "login succeeded, the app is broken."

These tests pin the opposite behaviour — fail loudly, hand back nothing the client
cannot use.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from user.models.user_session import UserSession

User = get_user_model()


class LoginSessionWriteTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("api-v1:login")
        self.password = "SecureActivePass123!"
        self.user = User.objects.create_user(
            email="ada@example.com", password=self.password, first_name="Ada"
        )
        self.user.is_active = True
        self.user.save()

    def login(self):
        return self.client.post(
            self.url,
            {"email": "ada@example.com", "password": self.password},
            format="json",
        )

    def test_a_successful_login_still_works(self):
        response = self.login()

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("access", response.data["data"])
        self.assertEqual(UserSession.objects.filter(user=self.user).count(), 1)

    def test_a_failed_session_write_returns_503_not_tokens(self):
        """The defect, named. Returning tokens here is worse than returning an error:
        the client stores them, believes it is signed in, and 401s on every call."""
        with patch(
            "user.services.sessions.create_for_tokens",
            side_effect=RuntimeError("database is gone"),
        ):
            response = self.login()

        self.assertEqual(response.status_code, 503, response.data)
        self.assertNotIn("access", response.data.get("data") or {})
        self.assertFalse(UserSession.objects.filter(user=self.user).exists())

    def test_a_failed_session_write_rolls_back_last_login(self):
        """The stamp and the row are one unit. A last_login that moved for a login
        that did not happen is a misleading audit trail."""
        self.user.refresh_from_db()
        before = self.user.last_login

        with patch(
            "user.services.sessions.create_for_tokens",
            side_effect=RuntimeError("database is gone"),
        ):
            self.login()

        self.user.refresh_from_db()
        self.assertEqual(self.user.last_login, before)

    def test_the_tokens_a_successful_login_returns_actually_authenticate(self):
        """End to end: the row exists and the access token resolves against it. This
        is the property the old failure mode silently broke."""
        response = self.login()
        access = response.data["data"]["access"]

        from rest_framework_simplejwt.tokens import AccessToken

        jti = AccessToken(access).get("jti")
        self.assertIsNotNone(UserSession.get_active_session(jti))


class RefreshSessionWriteTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.login_url = reverse("api-v1:login")
        self.refresh_url = reverse("api-v1:token_refresh")
        self.password = "SecureActivePass123!"
        self.user = User.objects.create_user(
            email="grace@example.com", password=self.password, first_name="Grace"
        )
        self.user.is_active = True
        self.user.save()

        login = self.client.post(
            self.login_url,
            {"email": "grace@example.com", "password": self.password},
            format="json",
        )
        self.refresh_token = login.data["data"]["refresh"]

    def test_a_successful_refresh_still_works(self):
        response = self.client.post(
            self.refresh_url, {"refresh": self.refresh_token}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("access", response.data["data"])

    def test_a_failed_rotation_write_returns_503_not_tokens(self):
        """The row would still point at the OLD jtis, so the pair just minted matches
        nothing and 401s on first use."""
        with patch.object(
            UserSession, "save", side_effect=RuntimeError("database is gone")
        ):
            response = self.client.post(
                self.refresh_url, {"refresh": self.refresh_token}, format="json"
            )

        self.assertEqual(response.status_code, 503, response.data)
        self.assertNotIn("access", response.data.get("data") or {})

    def test_the_refreshed_access_token_resolves_against_the_row(self):
        response = self.client.post(
            self.refresh_url, {"refresh": self.refresh_token}, format="json"
        )
        access = response.data["data"]["access"]

        from rest_framework_simplejwt.tokens import AccessToken

        jti = AccessToken(access).get("jti")
        self.assertIsNotNone(
            UserSession.get_active_session(jti),
            "rotation must leave the row pointing at the tokens it just issued",
        )


class VerifyPaymentSessionWriteTests(TestCase):
    """The post-payment sign-in has the same failure mode, at two call sites.

    Worth its own coverage: a mutation that made the view ignore the helper's error
    passed the whole onboarding suite, because nothing asserted the caller *acts* on
    it — only that it calls the helper at all.
    """

    class _Request:
        headers = {}
        META = {}

    def _view(self):
        from onboard.views.onboarding.verify_payment import VerifyPaymentView

        return VerifyPaymentView()

    def test_it_returns_a_503_response_on_failure(self):
        user = User.objects.create_user(email="paid@example.com", password="pw")

        with patch(
            "user.services.sessions.create_for_tokens",
            side_effect=RuntimeError("database is gone"),
        ):
            result = self._view()._create_user_session(
                user, self._Request(), {"jti": "r"}, {"jti": "a"}
            )

        self.assertIsNotNone(result, "a failed session write must not read as success")
        self.assertEqual(result.status_code, 503)

    def test_the_failure_message_says_the_payment_succeeded(self):
        """A generic error immediately after paying reads as a lost payment. The
        account and the charge are both fine at this point; only the auto-sign-in
        failed, and the message has to say so."""
        user = User.objects.create_user(email="paid2@example.com", password="pw")

        with patch(
            "user.services.sessions.create_for_tokens",
            side_effect=RuntimeError("database is gone"),
        ):
            result = self._view()._create_user_session(
                user, self._Request(), {"jti": "r"}, {"jti": "a"}
            )

        message = str(result.data.get("message", "")).lower()
        self.assertIn("payment was successful", message)
        self.assertIn("log in", message)

    def test_it_returns_none_on_success(self):
        user = User.objects.create_user(email="paid3@example.com", password="pw")

        result = self._view()._create_user_session(
            user, self._Request(), {"jti": "r-ok"}, {"jti": "a-ok"}
        )

        self.assertIsNone(result)
        self.assertTrue(UserSession.objects.filter(access_jti="a-ok").exists())

    def test_every_call_site_uses_the_returned_error(self):
        """The guard the structural test above was missing. Calling the helper and
        discarding its result restores the original defect — tokens returned after a
        failed session write — while still passing "does it call the helper"."""
        import inspect

        from onboard.views.onboarding import verify_payment

        source = inspect.getsource(verify_payment)
        calls = source.count("self._create_user_session(")
        assigned = source.count("= self._create_user_session(")

        self.assertEqual(
            calls,
            assigned,
            f"{calls - assigned} call site(s) discard the error _create_user_session "
            f"returns, so a failed session write would still hand back tokens.",
        )
        self.assertGreater(calls, 0)


class SharedHelperTests(TestCase):
    """One implementation, because two copies of the JTI extraction is how a session
    row silently stops matching its token."""

    def test_no_view_builds_a_session_row_by_hand(self):
        import inspect

        from onboard.views.onboarding import verify_payment
        from user.views.auth import login

        for module in (login, verify_payment):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertNotIn(
                    "UserSession.objects.create(",
                    source,
                    f"{module.__name__} builds a session row inline instead of using "
                    f"user.services.sessions.create_for_tokens — a second copy of the "
                    f"JTI extraction will drift from the first.",
                )
                self.assertIn("create_for_tokens", source)

    def test_the_helper_raises_rather_than_swallowing(self):
        """Its whole contract. A helper that logs and returns would reintroduce the
        defect at every call site at once."""
        from user.services import sessions as session_service

        user = User.objects.create_user(email="x@example.com", password="pw")

        class BrokenRequest:
            headers = {}
            META = {}

        with patch.object(
            UserSession.objects, "create", side_effect=RuntimeError("db gone")
        ):
            with self.assertRaises(RuntimeError):
                session_service.create_for_tokens(
                    user, BrokenRequest(), {"jti": "r"}, {"jti": "a"}
                )
