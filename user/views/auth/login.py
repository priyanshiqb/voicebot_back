from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from drf_spectacular.utils import extend_schema, OpenApiResponse

from utils.views.base_model_viewset import ResponseMixin
from user.serializers.auth import LoginSerializer, LoginResponseSerializer
from user.serializers.custom_user import UserRefSerializer
from onboard.serializers.onboarding import GeneralResponseSerializer


@extend_schema(
    tags=["Auth - Access"],
    request=LoginSerializer,
    responses={
        200: OpenApiResponse(
            response=LoginResponseSerializer,
            description="Login successful"
        ),
        400: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="Validation failed, invalid email/password."
        ),
        403: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="Account or IP locked due to too many failed login attempts."
        )
    }
)
class LoginAPIView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        from defender.utils import is_user_already_locked, is_source_ip_already_locked, get_ip

        email = request.data.get("email", "").strip().lower()
        ip = get_ip(request)

        if is_user_already_locked(email) or is_source_ip_already_locked(ip):
            return self.error(
                message="Account or IP temporarily locked due to too many failed login attempts. Please try again in 1 hour.",
                status_code=403
            )

        serializer = LoginSerializer(
            data=request.data
        )

        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        refresh = RefreshToken.for_user(user)
        access = refresh.access_token

        return self.success(
            data={
                "access": str(access),
                "refresh": str(refresh),
                "user": UserRefSerializer(user).data
            },
            message="Login successful"
        )
