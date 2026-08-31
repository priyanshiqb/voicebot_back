"""The two writes this milestone ships: profile edits and enforcement.

Same contract as the plan module — a named permission, a required reason, and an
audit row on every attempt including the failures. An operator repeatedly failing to
do something is a signal, and it disappears if only successes are kept.
"""

from django.test import TestCase

from onboard.models import Timezone
from onboard.tests.billing_factories import make_plan, make_tenant, subscribe
from ops.tests.tenant_console_factories import (
    Action,
    Role,
    audit_rows,
    client_for,
    console,
    detail_url,
    operator,
)
from tenant.models import TenantSettings
from user.models import OperatorAuditLog


class ProfileWriteTests(TestCase):
    def setUp(self):
        self.client_ops = console()
        self.ctx = make_tenant("acme")

    def test_manager_may_rename_a_tenant(self):
        response = self.client_ops.patch(
            detail_url(self.ctx.tenant),
            {"reason": "customer rebranded", "name": "Acme Industries"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.ctx.tenant.refresh_from_db()
        self.assertEqual(self.ctx.tenant.name, "Acme Industries")

    def test_a_reason_is_required(self):
        response = self.client_ops.patch(
            detail_url(self.ctx.tenant), {"name": "No Reason"}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("REASON_REQUIRED", response.json()["errors"])
        self.ctx.tenant.refresh_from_db()
        self.assertEqual(self.ctx.tenant.name, "acme")

    def test_a_blank_reason_is_not_a_reason(self):
        response = self.client_ops.patch(
            detail_url(self.ctx.tenant),
            {"reason": "   ", "name": "Whitespace"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_an_empty_patch_is_refused(self):
        """An audit row recording a change of nothing is worse than no row."""
        response = self.client_ops.patch(
            detail_url(self.ctx.tenant), {"reason": "nothing to do"}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_an_audit_row_records_what_changed_and_what_it_was(self):
        self.client_ops.patch(
            detail_url(self.ctx.tenant),
            {"reason": "typo at signup", "name": "Corrected"},
            format="json",
        )
        rows = audit_rows(Action.TENANT_PROFILE, self.ctx.tenant)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reason, "typo at signup")
        self.assertEqual(rows[0].payload["fields"], ["name"])
        self.assertEqual(rows[0].payload["before"]["name"], "acme")
        self.assertEqual(rows[0].outcome, OperatorAuditLog.Outcome.SUCCESS)
        self.assertEqual(rows[0].target, str(self.ctx.tenant.pk))

    def test_an_unknown_timezone_is_a_field_error_not_a_500(self):
        response = self.client_ops.patch(
            detail_url(self.ctx.tenant),
            {
                "reason": "moving them",
                "timezone": "00000000-0000-0000-0000-000000000000",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("timezone", response.json()["errors"])

    def test_timezone_can_be_corrected(self):
        """Built here rather than relying on seed data, so the test is self-contained."""
        other = Timezone.objects.create(
            offset="+05:30", region="Asia/Kolkata (console test)", timezone="IST",
        )
        response = self.client_ops.patch(
            detail_url(self.ctx.tenant),
            {"reason": "wrong region at signup", "timezone": str(other.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.ctx.tenant.refresh_from_db()
        self.assertEqual(self.ctx.tenant.timezone_id, other.pk)

    def test_a_deleted_tenant_cannot_be_edited(self):
        self.ctx.tenant.soft_delete()
        response = self.client_ops.patch(
            detail_url(self.ctx.tenant), {"reason": "why not", "name": "Zombie"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("TENANT_DELETED", response.json()["errors"])

    def test_the_response_is_the_whole_360(self):
        """The console holds one object; a write returns the same shape as a read."""
        response = self.client_ops.patch(
            detail_url(self.ctx.tenant), {"reason": "rename", "name": "Fresh"},
            format="json",
        )
        self.assertIn("health", response.json()["data"])
        self.assertEqual(response.json()["data"]["tenant"]["name"], "Fresh")


class ProfilePermissionTests(TestCase):
    def setUp(self):
        self.ctx = make_tenant("perm")

    def test_a_sub_manager_without_the_action_is_refused(self):
        client = client_for(operator("sub@ops.com", role=Role.SUB_MANAGER))
        response = client.patch(
            detail_url(self.ctx.tenant), {"reason": "nope", "name": "Denied"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.ctx.tenant.refresh_from_db()
        self.assertEqual(self.ctx.tenant.name, "perm")

    def test_a_sub_manager_granted_the_action_may_write(self):
        client = client_for(
            operator(
                "granted@ops.com",
                role=Role.SUB_MANAGER,
                permissions=[Action.TENANT_PROFILE],
            )
        )
        response = client.patch(
            detail_url(self.ctx.tenant), {"reason": "granted", "name": "Allowed"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_the_enforcement_grant_does_not_carry_over_to_a_profile_edit(self):
        """One vocabulary, one action each. Holding one is not holding the other."""
        client = client_for(
            operator(
                "partial@ops.com",
                role=Role.SUB_MANAGER,
                permissions=[Action.ENFORCEMENT_CHANGE],
            )
        )
        self.assertEqual(
            client.patch(
                detail_url(self.ctx.tenant), {"reason": "x", "name": "Nope"},
                format="json",
            ).status_code,
            403,
        )
        self.assertEqual(
            client.post(
                detail_url(self.ctx.tenant, "enforcement/"),
                {"reason": "metering checked", "credits": True},
                format="json",
            ).status_code,
            200,
        )

    def test_a_tenant_user_cannot_reach_a_write_at_all(self):
        response = self.ctx.client.patch(
            detail_url(self.ctx.tenant), {"reason": "self serve", "name": "Mine"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)


class EnforcementTests(TestCase):
    def setUp(self):
        self.client_ops = console()
        self.plan = make_plan("enforce")
        self.ctx = make_tenant("enforced")
        subscribe(self.ctx, self.plan)

    def url(self):
        return detail_url(self.ctx.tenant, "enforcement/")

    def test_turning_credits_enforcement_on(self):
        response = self.client_ops.post(
            self.url(),
            {"reason": "usage reviewed, rates confirmed", "credits": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        settings_row = TenantSettings.objects.get(tenant=self.ctx.tenant)
        self.assertTrue(settings_row.credits_enforced)
        self.assertFalse(settings_row.entitlements_enforced)

    def test_only_the_named_switch_moves(self):
        settings_row = TenantSettings.for_tenant(self.ctx.tenant.id)
        settings_row.entitlements_enforced = True
        settings_row.save(update_fields=["entitlements_enforced"])

        self.client_ops.post(
            self.url(), {"reason": "credits only", "credits": True}, format="json",
        )
        settings_row.refresh_from_db()
        self.assertTrue(settings_row.credits_enforced)
        self.assertTrue(settings_row.entitlements_enforced)

    def test_both_switches_at_once(self):
        self.client_ops.post(
            self.url(),
            {"reason": "graduating them", "credits": True, "entitlements": True},
            format="json",
        )
        settings_row = TenantSettings.objects.get(tenant=self.ctx.tenant)
        self.assertTrue(settings_row.credits_enforced)
        self.assertTrue(settings_row.entitlements_enforced)

    def test_turning_it_back_off(self):
        self.client_ops.post(
            self.url(), {"reason": "on", "credits": True}, format="json",
        )
        self.client_ops.post(
            self.url(), {"reason": "billing bug, unblocking them", "credits": False},
            format="json",
        )
        self.assertFalse(
            TenantSettings.objects.get(tenant=self.ctx.tenant).credits_enforced
        )

    def test_the_settings_row_is_created_on_first_write(self):
        """The one place a `for_tenant()` is right: a write, not a read."""
        bare = make_tenant("nosettings")
        self.assertFalse(TenantSettings.objects.filter(tenant=bare.tenant).exists())

        self.client_ops.post(
            detail_url(bare.tenant, "enforcement/"),
            {"reason": "first setting", "entitlements": True},
            format="json",
        )
        self.assertTrue(
            TenantSettings.objects.get(tenant=bare.tenant).entitlements_enforced
        )

    def test_a_reason_is_required(self):
        response = self.client_ops.post(self.url(), {"credits": True}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("REASON_REQUIRED", response.json()["errors"])

    def test_an_empty_body_is_refused(self):
        response = self.client_ops.post(
            self.url(), {"reason": "changed my mind"}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_the_audit_row_records_before_and_after(self):
        self.client_ops.post(
            self.url(), {"reason": "metering period over", "credits": True},
            format="json",
        )
        rows = audit_rows(Action.ENFORCEMENT_CHANGE, self.ctx.tenant)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].payload["before"]["credits"])
        self.assertTrue(rows[0].result["credits_enforced"])
        self.assertEqual(rows[0].tenant_id, self.ctx.tenant.id)

    def test_the_response_carries_the_updated_enforcement_block(self):
        response = self.client_ops.post(
            self.url(), {"reason": "on you go", "credits": True}, format="json",
        )
        block = response.json()["data"]["enforcement"]
        self.assertTrue(block["credits_enforced"])
        self.assertTrue(block["configured"])

    def test_a_deleted_tenant_cannot_have_enforcement_changed(self):
        self.ctx.tenant.soft_delete()
        response = self.client_ops.post(
            self.url(), {"reason": "why not", "credits": True}, format="json",
        )
        self.assertEqual(response.status_code, 409)


class ReadsAreNotAuditedTests(TestCase):
    """`OperatorAuditLog` records actions. Logging page views would bury them."""

    def test_opening_a_360_leaves_no_audit_row(self):
        client = console()
        ctx = make_tenant("watched")

        client.get(detail_url(ctx.tenant))
        client.get(detail_url(ctx.tenant, "health/"))
        client.get("/api/v1/ops/tenants/")

        self.assertEqual(OperatorAuditLog.objects.count(), 0)
