from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.contrib.auth import get_user_model

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser
from onboard.serializers.onboarding import CheckPhoneSerializer, CheckPhoneResponseSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema

User = get_user_model()


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Check Phone Availability (Unique Check)",
    description="Asynchronously checks if a phone number already exists in either the active CustomUser or pending TempUser tables. Returns availability status.",
    responses={
        200: CheckPhoneResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class CheckPhoneView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = CheckPhoneSerializer

    def post(self, request):
        serializer = CheckPhoneSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        phone_number = serializer.validated_data["phone_number"].strip()

        # Database index-optimized queries
        exists_in_active = User.all_objects.filter(phone_number=phone_number).exists()
        exists_in_pending = TempUser.objects.filter(phone_number=phone_number).exists()

        available = not (exists_in_active or exists_in_pending)

        return self.success(
            data={
                "phone_number": phone_number,
                "available": available,
                "exists_in_active_users": exists_in_active,
                "exists_in_pending_registrations": exists_in_pending
            },
            message="Phone number availability checked.",
            status_code=status.HTTP_200_OK
        )
