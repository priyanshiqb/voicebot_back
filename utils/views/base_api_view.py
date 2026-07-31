import logging
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from tenant.common.views.agent_tenant_scope import VBAgentActionPermission
from utils.views.base_model_viewset import ResponseMixin

logger = logging.getLogger(__name__)


class BaseAPIView(ResponseMixin, APIView):
    """
    Base APIView.
    Provides standardized JSON responses (status, message, data, errors) and
    defaults permissions to authenticated users.

    Response helpers to use:
    - Success Response:
        return self.success(data=serializer.data, message="Success message", status_code=status.HTTP_200_OK)
        
    - Error Response:
        return self.error(message="Error description", errors={"field": "details"}, status_code=status.HTTP_400_BAD_REQUEST)
    """
    # APIViews have no `action`, so VBAgentActionPermission denies machine callers here
    # unconditionally. That is intended: none of the endpoints on this base are part of
    # the voicebot surface, and a subclass that should be reachable must opt in
    # explicitly rather than inherit access.
    permission_classes = (IsAuthenticated, VBAgentActionPermission)
