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
