from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.contrib.auth.hashers import make_password
from django.core import signing

from onboard.models import TempUser
from onboard.serializers.onboarding import ResetPasswordSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding Signup Flow"],
    summary="Step 2 Helper: Reset Temp User Password",
    description="Resets the password of a temporary unverified onboarding registration using a secure one-time-use token.",
    responses={200: GeneralResponseSerializer}
)
class ResetPasswordView(APIView):
    permission_classes = [AllowAny]
    serializer_class = ResetPasswordSerializer

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                "status": False,
                "message": "Validation failed",
                "data": None,
                "errors": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        temp_id = serializer.validated_data["temp_registration_id"]
        token = serializer.validated_data["token"]
        new_password = serializer.validated_data["password"]

        try:
            temp_user = TempUser.objects.get(id=temp_id)
        except (TempUser.DoesNotExist, ValueError):
            return Response({
                "status": False,
                "message": "Invalid registration ID",
                "data": None,
                "errors": ["REGISTRATION_NOT_FOUND"]
            }, status=status.HTTP_400_BAD_REQUEST)

        # 1. Decode and verify the signed token with a max-age of 30 minutes (1800 seconds)
        try:
            data = signing.loads(token, max_age=1800)
        except signing.SignatureExpired:
            return Response({
                "status": False,
                "message": "The reset link has expired. Please request a new one.",
                "data": None,
                "errors": ["TOKEN_EXPIRED"]
            }, status=status.HTTP_400_BAD_REQUEST)
        except signing.BadSignature:
            return Response({
                "status": False,
                "message": "The reset link is invalid.",
                "data": None,
                "errors": ["INVALID_TOKEN"]
            }, status=status.HTTP_400_BAD_REQUEST)

        # 2. Scope checks
        if data.get("temp_user_id") != str(temp_user.id):
            return Response({
                "status": False,
                "message": "Token does not match this registration ID.",
                "data": None,
                "errors": ["INVALID_TOKEN_SCOPE"]
            }, status=status.HTTP_400_BAD_REQUEST)

        # 3. One-time usability check:
        # Compare token's password_hash with the database password_hash.
        # If they don't match, it means the password has already been reset/changed, making this token stale/used!
        if data.get("password_hash") != temp_user.password_hash:
            return Response({
                "status": False,
                "message": "This reset link has already been used.",
                "data": None,
                "errors": ["TOKEN_ALREADY_USED"]
            }, status=status.HTTP_400_BAD_REQUEST)

        # 4. Hash and save the new password
        temp_user.password_hash = make_password(new_password)
        temp_user.save()

        return Response({
            "status": True,
            "message": "Password reset successfully. You can now verify OTP or proceed with onboarding.",
            "data": None,
            "errors": None
        }, status=status.HTTP_200_OK)
