from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.core.mail import send_mail
from django.conf import settings
from django.core import signing

from onboard.models import TempUser
from onboard.serializers.onboarding import ForgotPasswordSerializer, GeneralResponseSerializer
from drf_spectacular.utils import extend_schema


@extend_schema(
    tags=["Onboarding Signup Flow"],
    summary="Step 2 Helper: Forgot Temp User Password",
    description="Initiates password reset for unverified onboarding temporary users. Emails a one-time secure link with a cryptographic token valid for 30 minutes.",
    responses={200: GeneralResponseSerializer}
)
class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]
    serializer_class = ForgotPasswordSerializer

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                "status": False,
                "message": "Validation failed",
                "data": None,
                "errors": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"].strip().lower()

        try:
            temp_user = TempUser.objects.get(email=email)
        except TempUser.DoesNotExist:
            return Response({
                "status": True,
                "message": "If the email is registered in our temporary signup system, a reset link will be sent shortly.",
                "data": None,
                "errors": None
            }, status=status.HTTP_200_OK)

        # 1. Cryptographically generate a one-time token incorporating the current password_hash.
        token = signing.dumps({
            "temp_user_id": str(temp_user.id),
            "password_hash": temp_user.password_hash
        }, key=settings.SECRET_KEY)

        # 2. Construct frontend redirect URL
        frontend_url = getattr(settings, "FRONTEND_ONBOARDING_RESET_URL", "http://localhost:3000/onboard/reset-password")
        reset_link = f"{frontend_url}?token={token}&temp_registration_id={temp_user.id}"

        # 3. Email the link
        try:
            send_mail(
                subject="Reset your Voicebot Onboarding Password",
                message=f"You requested to reset your password during onboarding. Use the following link to reset it. It is valid for 30 minutes:\n\n{reset_link}",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                recipient_list=[temp_user.email],
                fail_silently=False
            )
        except Exception as e:
            print(f"Password reset email delivery skipped/failed: {e}. Link: {reset_link}")

        return Response({
            "status": True,
            "message": "If the email is registered in our temporary signup system, a reset link will be sent shortly.",
            "data": {
                "temp_registration_id": str(temp_user.id),
                "token": token
            },
            "errors": None
        }, status=status.HTTP_200_OK)
