"""The admin console's foundation: who runs it, what they may do, what gets recorded.

Three things are pinned here, and all three fail silently if they break:

* **A tenant user must never be platform staff.** Not by default, not by holding
  Django admin rights, not by a half-configured role.
* **A sub-manager starts able to do nothing.** A new console action must be
  unreachable until somebody decides who should have it.
* **The audit log must be append-only, and must not be able to break the action it
  records.** Those pull in opposite directions and both matter.
"""

from django.db import IntegrityError, transaction
from django.test import TestCase

from user.models import CustomUser, OperatorAuditLog
from user.permissions import HasConsolePermission, IsPlatformStaff
from user.services import operator_audit

Role = CustomUser.PlatformRole
Action = OperatorAuditLog.Action


def make_user(email, **kwargs):
    return CustomUser.objects.create(email=email, username=email, **kwargs)


def make_operator(email, role=Role.MANAGER, permissions=None):
    return make_user(
        email,
        user_type=CustomUser.UserType.ADMIN,
        platform_role=role,
        console_permissions=permissions or [],
    )


class _Request:
    def __init__(self, user, meta=None):
        self.user = user
        self.META = meta or {}


class PlatformStaffTests(TestCase):
    def test_an_ordinary_tenant_user_is_not_platform_staff(self):
        user = make_user("tenant@example.com")

        self.assertIsNone(user.platform_role)
        self.assertFalse(user.is_platform_staff)

    def test_a_superuser_is_not_platform_staff_either(self):
        """`is_superuser` grants Django admin, which is engineering's escape hatch.
        The console is a different thing, and conflating them means every engineer
        silently gains operator access."""
        user = make_user("root@example.com", is_superuser=True, is_staff=True)

        self.assertFalse(user.is_platform_staff)

    def test_a_manager_is(self):
        manager = make_operator("manager@ops.com")

        self.assertTrue(manager.is_platform_staff)
        self.assertTrue(manager.is_console_manager)

    def test_a_sub_manager_is_staff_but_not_a_manager(self):
        sub = make_operator("sub@ops.com", Role.SUB_MANAGER)

        self.assertTrue(sub.is_platform_staff)
        self.assertFalse(sub.is_console_manager)

    def test_a_role_without_the_admin_user_type_is_refused_by_the_database(self):
        """Two markers for one fact is how they drift. A half-configured operator
        must not be able to exist — existing and being denied access for a reason
        nobody can find is worse than failing at creation."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_user("half@ops.com", platform_role=Role.MANAGER)

    def test_admin_user_type_without_a_role_is_allowed(self):
        """The constraint runs one way only. An ADMIN account with no role yet is a
        reasonable intermediate state; a role with no ADMIN is not."""
        user = make_user("pending@ops.com", user_type=CustomUser.UserType.ADMIN)

        self.assertFalse(user.is_platform_staff)


class ConsolePermissionTests(TestCase):
    def test_a_manager_may_do_everything(self):
        manager = make_operator("m@ops.com")

        for action in Action.values:
            self.assertTrue(manager.can(action), action)

    def test_a_manager_needs_no_grants(self):
        manager = make_operator("m2@ops.com")

        self.assertEqual(manager.console_permissions, [])
        self.assertTrue(manager.can(Action.CREDITS_GRANT))

    def test_a_sub_manager_starts_able_to_do_nothing(self):
        """Closed by default. A capability added next year must be unreachable
        until somebody decides who should have it, rather than appearing for
        everyone who already had a role."""
        sub = make_operator("s@ops.com", Role.SUB_MANAGER)

        for action in Action.values:
            self.assertFalse(sub.can(action), action)

    def test_a_sub_manager_may_do_what_was_granted(self):
        sub = make_operator(
            "s2@ops.com", Role.SUB_MANAGER, [Action.CREDITS_GRANT]
        )

        self.assertTrue(sub.can(Action.CREDITS_GRANT))
        self.assertFalse(sub.can(Action.CREDITS_DEDUCT))
        self.assertFalse(sub.can(Action.ENFORCEMENT_CHANGE))

    def test_a_tenant_user_can_do_nothing_at_all(self):
        self.assertFalse(make_user("t@example.com").can(Action.CREDITS_GRANT))

    def test_grants_are_idempotent(self):
        sub = make_operator("s3@ops.com", Role.SUB_MANAGER)

        sub.grant_console(Action.CREDITS_GRANT, Action.CREDITS_GRANT)
        sub.grant_console(Action.CREDITS_GRANT)

        self.assertEqual(sub.console_permissions, [Action.CREDITS_GRANT.value])

    def test_a_grant_can_be_revoked(self):
        sub = make_operator(
            "s4@ops.com", Role.SUB_MANAGER,
            [Action.CREDITS_GRANT, Action.WALLET_FREEZE],
        )

        sub.revoke_console(Action.CREDITS_GRANT)

        self.assertFalse(sub.can(Action.CREDITS_GRANT))
        self.assertTrue(sub.can(Action.WALLET_FREEZE))

    def test_revoking_something_never_held_is_harmless(self):
        sub = make_operator("s5@ops.com", Role.SUB_MANAGER)

        sub.revoke_console(Action.CREDITS_GRANT)

        self.assertEqual(sub.console_permissions, [])


class PermissionClassTests(TestCase):
    def test_the_gate_refuses_a_tenant_user(self):
        request = _Request(make_user("tenant@example.com"))
        self.assertFalse(IsPlatformStaff().has_permission(request, None))

    def test_the_gate_admits_an_operator(self):
        request = _Request(make_operator("m@ops.com"))
        self.assertTrue(IsPlatformStaff().has_permission(request, None))

    def test_a_required_permission_is_enforced(self):
        class View:
            required_permission = Action.CREDITS_GRANT

        granted = make_operator(
            "s@ops.com", Role.SUB_MANAGER, [Action.CREDITS_GRANT]
        )
        ungranted = make_operator("s2@ops.com", Role.SUB_MANAGER)
        manager = make_operator("m2@ops.com")

        self.assertTrue(HasConsolePermission().has_permission(_Request(granted), View()))
        self.assertFalse(
            HasConsolePermission().has_permission(_Request(ungranted), View())
        )
        self.assertTrue(HasConsolePermission().has_permission(_Request(manager), View()))

    def test_a_view_with_no_stated_permission_is_a_read(self):
        """Forgetting the attribute is "readable by staff", never "open to the
        world" — the namespace gate has already run."""
        class View:
            pass

        self.assertTrue(
            HasConsolePermission().has_permission(
                _Request(make_operator("s3@ops.com", Role.SUB_MANAGER)), View()
            )
        )
        self.assertFalse(
            HasConsolePermission().has_permission(
                _Request(make_user("t@example.com")), View()
            )
        )


class OperatorAuditLogTests(TestCase):
    def setUp(self):
        self.actor = make_operator("audit@ops.com")

    def entry(self, **kwargs):
        return operator_audit.record(
            actor=self.actor,
            action=Action.CREDITS_GRANT,
            reason="goodwill after an outage",
            payload={"amount": 500},
            **kwargs,
        )

    def test_it_records_what_was_done_and_why(self):
        row = self.entry(target="order_abc")

        self.assertEqual(row.actor, self.actor)
        self.assertEqual(row.action, Action.CREDITS_GRANT)
        self.assertEqual(row.reason, "goodwill after an outage")
        self.assertEqual(row.payload["amount"], 500)
        self.assertEqual(row.outcome, OperatorAuditLog.Outcome.SUCCESS)

    def test_failures_are_recorded_too(self):
        """An operator repeatedly failing to do something is a signal, and it
        vanishes entirely if only successes are kept."""
        row = operator_audit.failed(
            actor=self.actor,
            action=Action.PAYMENT_REPLAY,
            reason="customer says credits never arrived",
            error=ValueError("gateway unreachable"),
        )

        self.assertEqual(row.outcome, OperatorAuditLog.Outcome.FAILED)
        self.assertIn("gateway unreachable", row.result["error"])

    def test_the_client_address_is_captured(self):
        row = self.entry(request=_Request(self.actor, {
            "HTTP_X_FORWARDED_FOR": "203.0.113.9, 10.0.0.1",
            "HTTP_USER_AGENT": "console/1.0",
        }))

        # Left-most entry: the proxy appends, so the client is first.
        self.assertEqual(row.ip_address, "203.0.113.9")
        self.assertEqual(row.user_agent, "console/1.0")

    def test_permissions_and_the_log_share_one_vocabulary(self):
        """`can()` takes an Action code and the log stores an Action code. If those
        ever diverge, somebody can do a thing that is recorded as a different thing
        — or as nothing at all."""
        sub = make_operator(
            "s@ops.com", Role.SUB_MANAGER, [Action.WALLET_FREEZE]
        )
        self.assertTrue(sub.can(Action.WALLET_FREEZE))

        row = operator_audit.record(
            actor=sub, action=Action.WALLET_FREEZE, reason="abuse report"
        )

        self.assertEqual(row.action, Action.WALLET_FREEZE)
        self.assertIn(row.action, sub.console_permissions)

    # -- append-only -------------------------------------------------------- #

    def test_a_row_cannot_be_edited(self):
        row = self.entry()
        row.reason = "something more flattering"

        with self.assertRaises(NotImplementedError):
            row.save()

    def test_a_row_cannot_be_deleted(self):
        row = self.entry()
        with self.assertRaises(NotImplementedError):
            row.delete()

    def test_a_row_cannot_be_soft_deleted(self):
        """Worse than a hard delete: the default manager hides soft-deleted rows, so
        the entry would vanish from every audit view while still in the table."""
        row = self.entry()
        with self.assertRaises(NotImplementedError):
            row.soft_delete()

    def test_the_queryset_blocks_bulk_rewrites(self):
        self.entry()
        qs = OperatorAuditLog.objects.all()

        for call in (lambda: qs.delete(), lambda: qs.soft_delete(),
                     lambda: qs.update(reason="x")):
            with self.assertRaises(NotImplementedError):
                call()

    # -- and must never break the action it records ------------------------- #

    def test_a_broken_audit_write_does_not_raise(self):
        """An audit failure must not roll back a credit grant the customer is
        already expecting. That trades a missing row for a missing grant, which is
        the worse of the two."""
        from unittest.mock import patch

        with patch.object(
            OperatorAuditLog.objects, "create", side_effect=RuntimeError("db gone")
        ):
            with self.assertLogs("user.services.operator_audit", level="ERROR"):
                row = self.entry()

        self.assertIsNone(row)
