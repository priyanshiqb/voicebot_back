from rest_framework.permissions import BasePermission
from user.models.CustomUser import CustomUser

class IsVoicebotAgent(BasePermission):
    """
    Allows access only to voicebot agent users.
    """
    def has_permission(self, request, view):
        return bool(
            request.user and
            request.user.is_authenticated and
            request.user.user_type == CustomUser.UserType.VB_AGENT
        )


class IsPlatformStaff(BasePermission):
    """Gate for the admin console. Nothing under ``/api/v1/ops/`` runs without it.

    Reads ``platform_role``, **not** ``is_staff`` or ``is_superuser``. Those two grant
    Django admin, which is engineering's escape hatch and a different thing: a support
    agent should hold this permission and neither of those, and an engineer with a
    superuser account should not silently gain the console.
    """

    message = "This endpoint is for platform staff."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_platform_staff)


class HasConsolePermission(IsPlatformStaff):
    """Requires a specific console action, named on the view as ``required_permission``.

    A manager passes everything. A sub-manager passes what has been granted to them.
    The action code is one of ``OperatorAuditLog.Action`` — the same vocabulary the
    audit log uses, so what somebody may do and what is recorded when they do it
    cannot drift apart.

    A view that states no ``required_permission`` is treated as a **read**, and any
    platform staff may reach it. That is deliberate: the namespace gate has already
    run, so forgetting the attribute is "readable by staff", not "open to the world".
    Anything that changes state must name its permission — there is a test that
    every write endpoint does.
    """

    message = "Your console role does not include this action."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        required = getattr(view, "required_permission", None)
        if required is None:
            return True
        return request.user.can(required)
