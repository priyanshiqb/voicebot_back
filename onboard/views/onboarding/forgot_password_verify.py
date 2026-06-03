from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.conf import settings
from django.core import signing

from onboard.models import TempUser
from onboard.serializers.onboarding import ForgotPasswordVerifyOTPSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding - Password Reset"],
    summary="Step 2: Verify Forgot Password OTP",
    description="Verifies the emailed 6-digit OTP. Upon successful verification, invalidates the OTP and returns a secure temporary signed reset token.",
    responses={200: GeneralResponseSerializer}
)
class ForgotPasswordVerifyOTPView(APIView):
    permission_classes = [AllowAny]
    serializer_class = ForgotPasswordVerifyOTPSerializer

    def post(self, request):
        serializer = ForgotPasswordVerifyOTPSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                "status": False,
                "message": "Validation failed",
                "data": None,
                "errors": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        temp_id = serializer.validated_data["temp_registration_id"]
        otp_code = serializer.validated_data["otp_code"]

        try:
            temp_user = TempUser.objects.get(id=temp_id)
        except (TempUser.DoesNotExist, ValueError):
            return Response({
                "status": False,
                "message": "Invalid registration ID",
                "data": None,
                "errors": ["REGISTRATION_NOT_FOUND"]
            }, status=status.HTTP_400_BAD_REQUEST)

        if temp_user.otp_code != otp_code:
            return Response({
                "status": False,
                "message": "Invalid OTP code",
                "data": None,
                "errors": ["INVALID_OTP"]
            }, status=status.HTTP_400_BAD_REQUEST)

        if temp_user.is_otp_expired():
            return Response({
                "status": False,
                "message": "Verification code has expired",
                "data": None,
                "errors": ["OTP_EXPIRED"]
            }, status=status.HTTP_400_BAD_REQUEST)

        # Invalidate the OTP code in the database
        temp_user.otp_code = None
        temp_user.otp_created_at = None
        temp_user.save()

        # Generate a temporary signed reset token valid for 30 minutes (1800 seconds)
        token = signing.dumps({
            "temp_user_id": str(temp_user.id),
            "password_hash": temp_user.password_hash
        }, key=settings.SECRET_KEY)

        return Response({
            "status": True,
            "message": "OTP verified successfully",
            "data": {
                "token": token
            },
            "errors": None
        }, status=status.HTTP_200_OK)
