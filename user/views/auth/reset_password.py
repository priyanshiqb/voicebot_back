from django.core import signing
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from drf_spectacular.utils import extend_schema, OpenApiResponse

from utils.views.base_model_viewset import ResponseMixin
from user.models import CustomUser
from user.serializers.auth import UserResetPasswordSerializer
from onboard.serializers.onboarding import GeneralResponseSerializer


@extend_schema(
    tags=["Auth - Password Reset"],
    summary="Step 3: Reset Onboarded User Password",
    description="Accepts the temporary signed reset token and user ID to perform password updates, guaranteeing single-use token lifecycle.",
    request=UserResetPasswordSerializer,
    responses={
        200: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="Password reset successfully."
        ),
        400: OpenApiResponse(
            response=GeneralResponseSerializer,
            description="Validation or Token error."
        )
    }
)
class UserResetPasswordAPIView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = UserResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"].strip().lower()
        token = serializer.validated_data["token"]
        new_password = serializer.validated_data["password"]

        try:
            user = CustomUser.objects.get(email=email, is_active=True, is_deleted=False)
        except CustomUser.DoesNotExist:
            return self.error(
                message="Invalid email or user not found.",
                errors=["USER_NOT_FOUND"]
            )

        # Cryptographically verify the signed token
        try:
            payload = signing.loads(token, key=settings.SECRET_KEY, max_age=1800)
        except signing.SignatureExpired:
            return self.error(
                message="Reset token has expired.",
                errors=["TOKEN_EXPIRED"]
            )
        except signing.BadSignature:
            return self.error(
                message="Invalid token signature.",
                errors=["INVALID_TOKEN"]
            )

        # Ensure the token belongs to the specified user
        if payload.get("user_id") != str(user.id):
            return self.error(
                message="Token is not associated with this user.",
                errors=["INVALID_USER_TOKEN"]
            )

        # Ensure the token has not already been used (one-time usability scope check)
        if payload.get("password_hash") != user.password:
            return self.error(
                message="This reset link has already been used.",
                errors=["TOKEN_ALREADY_USED"]
            )

        # Perform password update
        user.set_password(new_password)
        user.save()

        return self.success(
            message="Password reset successfully."
        )
