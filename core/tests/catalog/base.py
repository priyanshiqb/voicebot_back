"""
Shared fixtures for the catalog tests.

Run the whole directory against Postgres — SQLite cannot create the `ArrayField`
columns, the GIN indexes, or the `Lower()` unique constraints these models depend
on, so on the default test database every test here fails for the wrong reason::

    python manage.py test core.tests.catalog --settings=config.settings.test_postgres
"""

from decimal import Decimal

from django.test import TestCase

from core.models.catalog_category import CatalogCategory
from core.models.catalog_item import CatalogItem
from core.models.catalog_variant import CatalogVariant
from tenant.models.Tenant import Tenant

#: `BaseModel` declares the audit FKs `null=True` but not `blank=True`, so
#: `full_clean()` reports all three as required on every Tier 2 model and no
#: validation test can ever reach the rule it is trying to exercise. Every
#: `full_clean()` call in this package excludes them.
AUDIT_FIELDS = ["created_by", "updated_by", "deleted_by"]


class CatalogTestCase(TestCase):
    """Two tenants, because most of the interesting rules are about the boundary
    between them: a constraint that is per-tenant must reject inside one tenant and
    permit across two, and testing only the first half passes with a global
    constraint."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-catalog")
        self.other_tenant = Tenant.objects.create(
            name="Globex", subdomain="globex-catalog"
        )

    # --- factories ------------------------------------------------------------

    def make_category(self, name="Beverages", tenant=None, **overrides):
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

    def make_variant(self, item=None, tenant=None, **overrides):
        item = item or self.make_item()
        fields = {
            "tenant": tenant or item.tenant,
            "item": item,
            "variant_label": "Standard",
            "pricing_type": CatalogVariant.PricingType.FIXED,
            # Decimal, not "100.00". Django does not coerce on assignment, so a string
            # here leaves a string on the in-memory instance — and any rule that later
            # compares it numerically (`validate_variant_pricing` reading an unchanged
            # field off the instance during a PATCH) raises TypeError. Fetched rows
            # always hold Decimals, so the fixture should too.
            "price": Decimal("100.00"),
        }
        fields.update(overrides)
        return CatalogVariant.objects.create(**fields)
