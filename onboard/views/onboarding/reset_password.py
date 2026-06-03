from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.contrib.auth.hashers import make_password
from django.core import signing

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser
from onboard.serializers.onboarding import ResetPasswordSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Password Reset"],
    summary="Step 3: Reset Temp User Password",
    description="Resets the password of a temporary unverified onboarding registration using a secure one-time-use token.",
    responses={
        200: GeneralResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class ResetPasswordView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = ResetPasswordSerializer

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        temp_id = serializer.validated_data["temp_registration_id"]
        token = serializer.validated_data["token"]
        new_password = serializer.validated_data["password"]

        try:
            temp_user = TempUser.objects.get(id=temp_id)
        except (TempUser.DoesNotExist, ValueError):
            return self.error(
                message="Invalid registration ID",
                errors=["REGISTRATION_NOT_FOUND"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # 1. Decode and verify the signed token with a max-age of 30 minutes (1800 seconds)
        try:
            data = signing.loads(token, max_age=1800)
        except signing.SignatureExpired:
            return self.error(
                message="The reset link has expired. Please request a new one.",
                errors=["TOKEN_EXPIRED"],
                status_code=status.HTTP_400_BAD_REQUEST
            )
        except signing.BadSignature:
            return self.error(
                message="The reset link is invalid.",
                errors=["INVALID_TOKEN"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # 2. Scope checks
        if data.get("temp_user_id") != str(temp_user.id):
            return self.error(
                message="Token does not match this registration ID.",
                errors=["INVALID_TOKEN_SCOPE"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # 3. One-time usability check:
        # Compare token's password_hash with the database password_hash.
        # If they don't match, it means the password has already been reset/changed, making this token stale/used!
        if data.get("password_hash") != temp_user.password_hash:
            return self.error(
                message="This reset link has already been used.",
                errors=["TOKEN_ALREADY_USED"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # 4. Hash and save the new password
        temp_user.password_hash = make_password(new_password)
        temp_user.save()

        return self.success(
            message="Password reset successfully. You can now verify OTP or proceed with onboarding.",
            status_code=status.HTTP_200_OK
        )

