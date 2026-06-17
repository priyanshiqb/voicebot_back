from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.contrib.auth import get_user_model

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser
from onboard.serializers.onboarding import CheckEmailSerializer, CheckEmailResponseSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema

User = get_user_model()


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Check Email Availability (Unique Check)",
    description="Asynchronously checks if an email address already exists in either the active CustomUser or pending TempUser tables. Returns availability status with zero latency for interactive typing feedback.",
    responses={
        200: CheckEmailResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class CheckEmailView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = CheckEmailSerializer

    def post(self, request):
        serializer = CheckEmailSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        email = serializer.validated_data["email"].strip().lower()

        # Step 1: Query Bloom Filter (Fast Path)
        from utils.bloom_filter import RedisBloomFilter
        bf = RedisBloomFilter()
        if not bf.exists(email):
            return self.success(
                data={
                    "email": email,
                    "available": True,
                    "exists_in_active_users": False,
                    "exists_in_pending_registrations": False
                },
                message="Email availability checked (fast-path).",
                status_code=status.HTTP_200_OK
            )

        # Database index-optimized queries (Slow Path - handles potential false positives)
        exists_in_active = User.objects.filter(email=email).exists()
        exists_in_pending = TempUser.objects.filter(email=email).exists()

        available = not (exists_in_active or exists_in_pending)

        return self.success(
            data={
                "email": email,
                "available": available,
                "exists_in_active_users": exists_in_active,
                "exists_in_pending_registrations": exists_in_pending
            },
            message="Email availability checked (slow-path).",
            status_code=status.HTTP_200_OK
        )

