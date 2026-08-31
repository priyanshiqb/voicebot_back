"""Shared setup for the tenant-console suite.

Operator accounts and the console client, built once. The tenant-side world comes
from ``onboard.tests.billing_factories`` — this deliberately does not rebuild it,
because a console test asserting on its own fixtures rather than on the real billing
state is a console test that passes while the console is wrong.
"""

from rest_framework.test import APIClient

from user.models import CustomUser, OperatorAuditLog

Role = CustomUser.PlatformRole
Action = OperatorAuditLog.Action

TENANTS = "/api/v1/ops/tenants/"


def detail_url(tenant, suffix=""):
    return f"{TENANTS}{tenant.id}/{suffix}"


def operator(email, role=Role.MANAGER, permissions=None):
    """A platform staff account.

    ``user_type = ADMIN`` is not decoration: a database constraint ties
    ``platform_role`` to it, so an operator built without it cannot be saved.
    """
    return CustomUser.objects.create(
        email=email,
        username=email,
        user_type=CustomUser.UserType.ADMIN,
        platform_role=role,
        console_permissions=permissions or [],
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def console(email="ops@example.com", role=Role.MANAGER, permissions=None):
    """An authenticated console client, for the common case."""
    return client_for(operator(email, role=role, permissions=permissions))


def audit_rows(action=None, tenant=None):
    rows = OperatorAuditLog.objects.all()
    if action is not None:
        rows = rows.filter(action=action)
    if tenant is not None:
        rows = rows.filter(tenant=tenant)
    return list(rows)
