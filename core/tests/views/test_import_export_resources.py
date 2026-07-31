"""
Regression tests for the shared import/export resource base.

`TenantModelResource.before_save_instance` carried the pre-4.x signature
`(instance, using_transactions, dry_run)`, but import-export 4.x calls it as
`(instance, row, **kwargs)` — so every call raised TypeError and the `/import/`
endpoint returned 500 for every resource using it.

It also gated `created_by` on `if not instance.pk`, which never fires: every model here
defaults its pk to `uuid4`, so a brand-new unsaved object already has one.
"""

import inspect

from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from core.models.agent_template import AgentTemplate
from core.resources.base_resource import TenantModelResource
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping
from user.models import CustomUser as User


class ResourceSignatureTests(APITestCase):
    def test_signature_matches_import_export_4x(self):
        """The base class must accept how the library actually calls it."""
        from import_export import resources

        library = inspect.signature(resources.Resource.before_save_instance)
        ours = inspect.signature(TenantModelResource.before_save_instance)

        self.assertEqual(list(ours.parameters)[:3], list(library.parameters)[:3])
        # **kwargs must be accepted, or extra keywords blow up the call.
        self.assertTrue(
            any(p.kind is inspect.Parameter.VAR_KEYWORD for p in ours.parameters.values())
        )


class TenantModelResourceBehaviourTests(APITestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-res")
        self.user = User.objects.create_user(
            username="res", email="res@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)

    def _resource(self):
        from core.resources.agent_template import AgentTemplateResource

        resource = AgentTemplateResource()
        resource.tenant = self.tenant
        resource.user = self.user
        return resource

    def test_injects_tenant_and_audit_fields_on_create(self):
        resource = self._resource()
        instance = AgentTemplate(name="Imported Template")

        # New object: pk is already populated by the uuid4 default, which is exactly
        # why `if not instance.pk` could never detect creation.
        self.assertIsNotNone(instance.pk)
        self.assertTrue(instance._state.adding)

        resource.before_save_instance(instance, {"name": "Imported Template"})

        self.assertEqual(instance.tenant_id, self.tenant.id)
        self.assertEqual(instance.created_by_id, self.user.id)
        self.assertEqual(instance.updated_by_id, self.user.id)

    def test_does_not_reassign_created_by_on_update(self):
        other = User.objects.create_user(
            username="orig", email="orig@example.com", password="password"
        )
        existing = AgentTemplate.objects.create(
            tenant=self.tenant, name="Existing", created_by=other, updated_by=other
        )
        existing.refresh_from_db()
        self.assertFalse(existing._state.adding)

        resource = self._resource()
        resource.before_save_instance(existing, {"name": "Existing"})

        self.assertEqual(existing.created_by_id, other.id)
        self.assertEqual(existing.updated_by_id, self.user.id)

    def test_does_not_clobber_an_existing_tenant(self):
        other_tenant = Tenant.objects.create(name="Globex", subdomain="globex-res")
        instance = AgentTemplate(name="Scoped", tenant=other_tenant)

        self._resource().before_save_instance(instance, {"name": "Scoped"})

        self.assertEqual(instance.tenant_id, other_tenant.id)

    def test_audit_assignment_is_skipped_on_models_without_those_fields(self):
        """
        Tier 1 (Core) models have no audit columns. Assigning them is a silent no-op,
        so the base class checks before writing.
        """
        from core.models.device_token import DeviceToken

        resource = self._resource()
        token = DeviceToken(user=self.user, token="abc", platform=DeviceToken.Platform.WEB)

        resource.before_save_instance(token, {"token": "abc"})

        self.assertEqual(token.tenant_id, self.tenant.id)
        self.assertFalse(hasattr(token, "created_by_id"))


class ImportEndpointTests(APITestCase):
    """The endpoint that was returning 500 because of the signature mismatch."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-imp-ep")
        self.user = User.objects.create_user(
            username="impep", email="impep@example.com", password="password"
        )
        TenantUserMapping.objects.create(user=self.user, tenant=self.tenant)
        self.client_user = APIClient()
        self.client_user.force_authenticate(user=self.user)

    @staticmethod
    def _upload(csv_bytes):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile("templates.csv", csv_bytes, content_type="text/csv")

    def test_agent_template_import_succeeds(self):
        """A plain CSV with no id column must import as new records, not error."""
        csv_bytes = (
            b"name,short_description,industry,strict_facts,can_user_edit_flow,is_active\n"
            b"Imported One,Desc,Tech,False,True,True\n"
        )
        response = self.client_user.post(
            "/api/v1/core/agent-templates/import/",
            {"file": self._upload(csv_bytes)},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        created = AgentTemplate.objects.filter(name="Imported One").first()
        self.assertIsNotNone(created)
        self.assertEqual(created.tenant_id, self.tenant.id)
        self.assertEqual(created.created_by_id, self.user.id)

    def test_import_cannot_update_another_tenants_record_by_id(self):
        """
        Declaring `id` lets a CSV target an existing row. Instance lookup is scoped to
        the caller's tenant, so a crafted id cannot reach across the boundary.
        """
        victim_tenant = Tenant.objects.create(name="Globex", subdomain="globex-imp-ep")
        victim = AgentTemplate.objects.create(
            tenant=victim_tenant, name="Victim Template"
        )

        csv_bytes = (
            b"id,name,short_description,industry,strict_facts,can_user_edit_flow,is_active\n"
            + str(victim.id).encode()
            + b",Hijacked,Desc,Tech,False,True,True\n"
        )
        response = self.client_user.post(
            "/api/v1/core/agent-templates/import/",
            {"file": self._upload(csv_bytes)},
            format="multipart",
        )

        # The row is treated as new for this tenant rather than updating the victim.
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        victim.refresh_from_db()
        self.assertEqual(victim.name, "Victim Template")
        self.assertEqual(victim.tenant_id, victim_tenant.id)

    def test_import_reports_actual_counts_not_file_row_count(self):
        """`len(dataset)` is the file's row count, not what was written."""
        csv_bytes = (
            b"name,short_description,industry,strict_facts,can_user_edit_flow,is_active\n"
            b"Count One,Desc,Tech,False,True,True\n"
            b"Count Two,Desc,Tech,False,True,True\n"
        )
        response = self.client_user.post(
            "/api/v1/core/agent-templates/import/",
            {"file": self._upload(csv_bytes)},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["data"]["created"], 2)
        self.assertEqual(response.data["data"]["updated"], 0)

    def test_invalid_rows_are_reported_not_silently_skipped(self):
        """
        A row that collides with an existing record must be reported, not swallowed.

        This held before the mixin rewrite too (the collision surfaces as a row error);
        it is kept as a guard on the behaviour, not as proof of a past defect.
        """
        AgentTemplate.objects.create(tenant=self.tenant, name="Taken Name")

        csv_bytes = (
            b"name,short_description,industry,strict_facts,can_user_edit_flow,is_active\n"
            b"Taken Name,Desc,Tech,False,True,True\n"
        )
        response = self.client_user.post(
            "/api/v1/core/agent-templates/import/",
            {"file": self._upload(csv_bytes)},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(response.data["errors"])
        # Nothing extra written.
        self.assertEqual(
            AgentTemplate.objects.filter(tenant=self.tenant, name="Taken Name").count(), 1
        )

    def test_a_bad_row_rolls_back_the_whole_import(self):
        """
        All-or-nothing: a half-applied bulk import is worse than a rejected one.

        The dry-run gate already prevented this in the common case; the explicit
        use_transactions=True makes it hold rather than depending on the unset
        IMPORT_EXPORT_USE_TRANSACTIONS global.
        """
        AgentTemplate.objects.create(tenant=self.tenant, name="Duplicate")

        csv_bytes = (
            b"name,short_description,industry,strict_facts,can_user_edit_flow,is_active\n"
            b"Good New One,Desc,Tech,False,True,True\n"
            b"Duplicate,Desc,Tech,False,True,True\n"
        )
        response = self.client_user.post(
            "/api/v1/core/agent-templates/import/",
            {"file": self._upload(csv_bytes)},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The valid row must not have been left behind.
        self.assertFalse(
            AgentTemplate.objects.filter(tenant=self.tenant, name="Good New One").exists()
        )

    def test_import_updates_own_record_by_id(self):
        existing = AgentTemplate.objects.create(tenant=self.tenant, name="Before")

        csv_bytes = (
            b"id,name,short_description,industry,strict_facts,can_user_edit_flow,is_active\n"
            + str(existing.id).encode()
            + b",After,Desc,Tech,False,True,True\n"
        )
        response = self.client_user.post(
            "/api/v1/core/agent-templates/import/",
            {"file": self._upload(csv_bytes)},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        existing.refresh_from_db()
        self.assertEqual(existing.name, "After")
        self.assertEqual(AgentTemplate.objects.filter(tenant=self.tenant).count(), 1)


class ImportRequiresATenantTests(APITestCase):
    """
    `import_data` never calls the viewset's `get_queryset`, so the tenant guard that
    protects every other write on these viewsets does not run on the import path.

    The only thing standing between a caller and another tenant's rows was
    `TenantModelResource.get_queryset`, and it skipped its filter whenever `tenant` was
    unset — reading an absent scope as "no scope needed". A VB_AGENT token has no tenant
    mapping, so it hit exactly that branch and could overwrite any tenant's records by
    putting their ids in a CSV. It was verified doing so on agents, agent-templates and
    probing-questions before this was fixed.
    """

    def setUp(self):
        self.victim_tenant = Tenant.objects.create(
            name="Globex", subdomain="globex-noten"
        )
        self.victim = AgentTemplate.objects.create(
            tenant=self.victim_tenant, name="Victim Template"
        )

        self.runner = User.objects.create_user(
            username="vb_noten",
            email="vb-noten@example.com",
            password="password",
            user_type=User.UserType.VB_AGENT,
        )
        self.client_vb = APIClient()
        self.client_vb.force_authenticate(user=self.runner)

    def _hijack_csv(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        csv_bytes = (
            b"id,name,short_description,industry,strict_facts,can_user_edit_flow,is_active\n"
            + str(self.victim.id).encode()
            + b",PWNED,Desc,Tech,False,True,True\n"
        )
        return SimpleUploadedFile("templates.csv", csv_bytes, content_type="text/csv")

    def test_vb_agent_cannot_import(self):
        response = self.client_vb.post(
            "/api/v1/core/agent-templates/import/",
            {"file": self._hijack_csv()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.victim.refresh_from_db()
        self.assertEqual(self.victim.name, "Victim Template")

    def test_vb_agent_cannot_export(self):
        response = self.client_vb.get("/api/v1/core/agent-templates/export/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_superusers_keep_their_global_carve_out(self):
        """
        Superusers have no tenant mapping either, so the fix must not lock them out —
        TenantBaseViewSet exempts them everywhere else and import is no different.
        """
        from django.core.files.uploadedfile import SimpleUploadedFile

        admin = User.objects.create_superuser(
            username="root_imp", email="root-imp@example.com", password="password"
        )
        client_admin = APIClient()
        client_admin.force_authenticate(user=admin)

        csv_bytes = (
            b"id,name,short_description,industry,strict_facts,can_user_edit_flow,is_active\n"
            + str(self.victim.id).encode()
            + b",Renamed By Admin,Desc,Tech,False,True,True\n"
        )
        response = client_admin.post(
            "/api/v1/core/agent-templates/import/",
            {"file": SimpleUploadedFile("t.csv", csv_bytes, content_type="text/csv")},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.victim.refresh_from_db()
        self.assertEqual(self.victim.name, "Renamed By Admin")


class ResourceFailsClosedTests(APITestCase):
    """`get_queryset` is the boundary, so its unset-tenant behaviour is pinned directly."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", subdomain="acme-failclosed")
        AgentTemplate.objects.create(tenant=self.tenant, name="Existing")

    @staticmethod
    def _resource():
        from core.resources.agent_template import AgentTemplateResource

        return AgentTemplateResource()

    def test_unset_tenant_matches_nothing(self):
        resource = self._resource()

        self.assertEqual(resource.tenant, None)
        self.assertFalse(resource.allow_cross_tenant)
        self.assertEqual(resource.get_queryset().count(), 0)

    def test_scoped_tenant_sees_only_its_own(self):
        other = Tenant.objects.create(name="Globex", subdomain="globex-failclosed")
        AgentTemplate.objects.create(tenant=other, name="Theirs")

        resource = self._resource()
        resource.tenant = self.tenant

        names = set(resource.get_queryset().values_list("name", flat=True))
        self.assertEqual(names, {"Existing"})

    def test_cross_tenant_is_opt_in(self):
        other = Tenant.objects.create(name="Globex", subdomain="globex-optin")
        AgentTemplate.objects.create(tenant=other, name="Theirs")

        resource = self._resource()
        resource.allow_cross_tenant = True

        self.assertEqual(resource.get_queryset().count(), 2)
