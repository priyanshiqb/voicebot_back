from rest_framework.permissions import BasePermission


class IsWithinLoginWindow(BasePermission):

    message = (
        "Access is not allowed at this time. Please login within your allowed time window."
    )

    def has_permission(self, request, view):

        user = request.user

        # Unauthenticated user
        if not user or not user.is_authenticated:
            return True
        
        return user.can_login_now()