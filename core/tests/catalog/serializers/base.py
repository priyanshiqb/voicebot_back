"""
Shared setup for the catalog serializer tests.

Serializers here resolve the caller's tenant off the request — it is not in the
payload, because `TenantBaseViewSet.perform_create` injects it *after* validation.
So every test needs a request whose user carries a `tenant_mapping`, and the
per-tenant uniqueness and cross-tenant ownership rules cannot be exercised without
one. That plumbing is what this module exists for.

Two tenants, each with a user, because most of the interesting rules are about the
boundary: a check that is per-tenant must reject inside one tenant and permit across
two, and testing only the first half passes with a global check.

Run against Postgres::

    python manage.py test core.tests.catalog --settings=config.settings.test_postgres
"""

from django.contrib.auth import get_user_model
from django.test import RequestFactory

from core.tests.catalog.base import CatalogTestCase
from tenant.models.TenantUserMapping import TenantUserMapping

User = get_user_model()


class CatalogSerializerTestCase(CatalogTestCase):
    def setUp(self):
        super().setUp()

        self.user = User.objects.create_user(
            username="alex", email="alex@example.com", password="password"
        )
        self.other_user = User.objects.create_user(
            username="bob", email="bob@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        TenantUserMapping.objects.create(user=self.other_user, tenant=self.other_tenant)

        factory = RequestFactory()
        self.request = factory.post("/api/v1/core/catalog-items/")
        self.request.user = self.user
        self.other_request = factory.post("/api/v1/core/catalog-items/")
        self.other_request.user = self.other_user

    # --- helpers --------------------------------------------------------------

    def context(self, other_tenant=False):
        return {"request": self.other_request if other_tenant else self.request}

    def build(self, serializer_class, data, instance=None, partial=False, other_tenant=False):
        """A bound serializer with the request context already wired."""
        return serializer_class(
            instance=instance,
            data=data,
            partial=partial,
            context=self.context(other_tenant=other_tenant),
        )

    def assert_field_error(self, serializer, field, contains=None):
        """Assert the serializer failed *on a named field*, not anywhere.

        The distinction matters: a rule that lands in `non_field_errors` renders as a
        banner the user cannot act on, while the same message on `price` highlights the
        input they need to fix. Several rules here exist specifically to move an error
        from one to the other, so asserting the field is the assertion.
        """
        self.assertFalse(serializer.is_valid(), f"expected an error on '{field}'")
        self.assertIn(
            field,
            serializer.errors,
            f"expected an error on '{field}', got {dict(serializer.errors)}",
        )
        if contains is not None:
            joined = " ".join(str(msg) for msg in serializer.errors[field])
            self.assertIn(contains, joined)
        return serializer.errors

    def save_valid(self, serializer, **extra):
        """Validate, then save the way `perform_create` does.

        `tenant`, `created_by` and `updated_by` are injected by the viewset after
        validation, never accepted from the payload, so a serializer test that omits
        them is testing a save path that cannot happen in production.
        """
        self.assertTrue(serializer.is_valid(), dict(serializer.errors))
        return serializer.save(
            tenant=self.tenant,
            created_by=self.user,
            updated_by=self.user,
            **extra,
        )
