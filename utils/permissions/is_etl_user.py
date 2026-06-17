from rest_framework.permissions import BasePermission
from user.models import CustomUser


class IsETLUser(BasePermission):
    """
    Allows access only to users with user_type set to ETL.
    """

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "user_type", None) == CustomUser.UserType.ETL
        )
