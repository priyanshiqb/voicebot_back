"""
Shared setup for the catalog API tests.

Three callers, because the three things most worth asserting about a tenant-scoped
API are about who is asking:

* **alex** — an ordinary user in tenant A. The happy path.
* **bob** — an ordinary user in tenant B. Everything of alex's must be **404** to
  him, never 403: a 403 confirms the row exists, which is itself a leak.
* **machine** — a `VB_AGENT` token. None of these viewsets declares
  `vb_agent_allowed_actions`, so every action is refused. That default is
  closed-by-default on purpose, and a viewset that silently opened up would
  otherwise go unnoticed.

Run against Postgres::

    python manage.py test core.tests.catalog --settings=config.settings.test_postgres
"""

from rest_framework.test import APIClient, APITestCase

from core.models.catalog_category import CatalogCategory
from core.models.catalog_item import CatalogItem
from core.models.catalog_variant import CatalogVariant
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User

CATEGORIES = "/api/v1/core/catalog-categories/"
ITEMS = "/api/v1/core/catalog-items/"
VARIANTS = "/api/v1/core/catalog-variants/"
MEDIA = "/api/v1/core/catalog-item-media/"


class CatalogAPITestCase(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-catalog-api")
        self.other_tenant = Tenant.objects.create(
            name="Globex", subdomain="globex-catalog-api"
        )

        self.user = User.objects.create_user(
            username="alex", email="alex@example.com", password="pw"
        )
        self.other_user = User.objects.create_user(
            username="bob", email="bob@example.com", password="pw"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.other_user, tenant=self.other_tenant)

        self.machine_user = User.objects.create_user(
            username="runner", email="runner@example.com", password="pw"
        )
        self.machine_user.user_type = User.UserType.VB_AGENT
        self.machine_user.save(update_fields=["user_type"])

        self.api = APIClient()
        self.api.force_authenticate(user=self.user)
        self.api_other = APIClient()
        self.api_other.force_authenticate(user=self.other_user)
        self.api_machine = APIClient()
        self.api_machine.force_authenticate(user=self.machine_user)
        self.api_anon = APIClient()

    # --- factories ------------------------------------------------------------

    def make_category(self, name="Menu", tenant=None, **overrides):
        fields = {"tenant": tenant or self.tenant, "name": name}
        fields.update(overrides)
        return CatalogCategory.objects.create(**fields)

    def make_item(self, name="Filter Coffee", tenant=None, **overrides):
        fields = {
            "tenant": tenant or self.tenant,
            "name": name,
            "item_kind": CatalogItem.ItemKind.PHYSICAL_GOOD,
        }
        fields.update(overrides)
        return CatalogItem.objects.create(**fields)

    def make_variant(self, item=None, **overrides):
        item = item or self.make_item()
        fields = {
            "tenant": item.tenant,
            "item": item,
            "variant_label": "Standard",
            "pricing_type": CatalogVariant.PricingType.FIXED,
            "price": "100.00",
        }
        fields.update(overrides)
        return CatalogVariant.objects.create(**fields)

    # --- assertions -----------------------------------------------------------

    def assert_ok(self, response, status_code=200):
        self.assertEqual(response.status_code, status_code, response.data)
        self.assertTrue(response.data["status"], response.data)
        return response.data["data"]

    def assert_error(self, response, status_code=400, field=None, contains=None):
        """Assert a failure, optionally on a named field.

        The field matters: an error in `errors.<field>` renders next to the input the
        user has to fix, while the same message in a bare `message` is a banner they
        cannot act on.
        """
        self.assertEqual(response.status_code, status_code, response.data)
        self.assertFalse(response.data.get("status", False), response.data)
        if field is not None:
            errors = response.data.get("errors") or {}
            self.assertIn(field, errors, response.data)
            if contains is not None:
                self.assertIn(contains, " ".join(str(m) for m in errors[field]))
        elif contains is not None:
            self.assertIn(contains, str(response.data.get("message")))
        return response.data

    def assert_paginated(self, response, count=None):
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("meta", response.data)
        for key in ("count", "next", "previous", "page", "page_size"):
            self.assertIn(key, response.data["meta"])
        if count is not None:
            self.assertEqual(response.data["meta"]["count"], count, response.data)
        return response.data["data"]
