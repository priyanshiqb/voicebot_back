import logging
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
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
    permission_classes = (IsAuthenticated,)
