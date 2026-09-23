"""
Who can read the API documentation.

The schema is a complete map of the API — every endpoint, field, enum and error — so
anonymous callers get nothing. `/api/internal/...` is always registered and always asks
for an account holding the documentation permission; the familiar unauthenticated paths
exist only where a deployment turns them on.
"""

import importlib

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import clear_url_caches

import config.urls

User = get_user_model()

PASSWORD = "SecurePassword123!"
INTERNAL = (
    "/api/internal/schema/",
    "/api/internal/docs/",
    "/api/internal/redoc/",
)
PUBLIC = ("/api/schema/", "/api/docs/", "/api/redoc/")


class ApiDocsAccessTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(
            email="viewer@example.com", password=PASSWORD
        )
        self.viewer.user_permissions.add(
            Permission.objects.get(codename="view_api_docs")
        )
        self.outsider = User.objects.create_user(
            email="outsider@example.com", password=PASSWORD
        )
        self.root = User.objects.create_superuser(
            username="root", email="root@example.com", password=PASSWORD
        )

    def _status(self, url):
        return self.client.get(url).status_code

    # ------------------------------------------------------------- refused

    def test_anonymous_callers_get_nothing(self):
        for url in INTERNAL:
            with self.subTest(url=url):
                self.assertEqual(self._status(url), 403)

    def test_signing_in_is_not_enough_without_the_permission(self):
        self.client.force_login(self.outsider)

        for url in INTERNAL:
            with self.subTest(url=url):
                self.assertEqual(self._status(url), 403)

    # ------------------------------------------------------------- allowed

    def test_the_permission_opens_all_three(self):
        self.client.force_login(self.viewer)

        for url in INTERNAL:
            with self.subTest(url=url):
                self.assertEqual(self._status(url), 200)

    def test_a_superuser_holds_every_permission(self):
        self.client.force_login(self.root)

        self.assertEqual(self._status("/api/internal/docs/"), 200)

    def test_the_schema_route_returns_the_openapi_document(self):
        self.client.force_login(self.viewer)

        response = self.client.get("/api/internal/schema/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"openapi", response.content)

    # ------------------------------------------------------------- the public paths

    def test_the_unauthenticated_paths_do_not_exist_when_disabled(self):
        """Not "forbidden" — not routed at all, so nothing answers on them.

        The URLconf decides this when it is imported, so the setting is flipped and the
        real `config.urls` reloaded: asserting against a copy of the branch would prove
        only that the copy is right.
        """
        with override_settings(API_DOCS_ENABLED=False):
            self._reload_urls()
            try:
                for url in PUBLIC:
                    with self.subTest(url=url):
                        self.assertEqual(self._status(url), 404)
                # The permission-gated ones are unaffected: they are always registered.
                self.assertEqual(self._status("/api/internal/docs/"), 403)
            finally:
                self._reload_urls()

    @staticmethod
    def _reload_urls():
        importlib.reload(config.urls)
        clear_url_caches()

    @override_settings(ROOT_URLCONF="user.tests.urls_docs_enabled")
    def test_a_deployment_may_turn_the_unauthenticated_paths_on(self):
        for url in PUBLIC:
            with self.subTest(url=url):
                self.assertEqual(self._status(url), 200)
