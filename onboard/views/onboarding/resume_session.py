from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.utils import timezone
from utils.tasks import send_email_task
from django.conf import settings
from django.contrib.auth import get_user_model
import random

from utils.views.base_model_viewset import ResponseMixin
from onboard.models import TempUser, BillingAddress, Transaction
from onboard.serializers.onboarding import (
    BillingAddressSerializer,
    ResumeSessionSerializer,
    ResumeSessionResponseSerializer,
    GeneralResponseSerializer,
)
from onboard.authentication import generate_temp_token
from drf_spectacular.utils import extend_schema

User = get_user_model()


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 2 Helper: Resume Onboarding Session",
    description="Allows a user to resume their onboarding process from their last completed state by providing their email or registration ID.",
    responses={
        200: ResumeSessionResponseSerializer,
        400: GeneralResponseSerializer
    }
)
class ResumeSessionView(ResponseMixin, APIView):
    permission_classes = [AllowAny]
    serializer_class = ResumeSessionSerializer

    def post(self, request):
        serializer = ResumeSessionSerializer(data=request.data)
        if not serializer.is_valid():
            return self.error(
                message="Validation failed",
                errors=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST
            )

        email = serializer.validated_data.get("email")
        temp_id = serializer.validated_data.get("temp_registration_id")
        password = serializer.validated_data.get("password")

        temp_user = None
        if temp_id:
            try:
                temp_user = TempUser.objects.get(id=temp_id)
            except (TempUser.DoesNotExist, ValueError):
                pass
        elif email:
            temp_user = TempUser.objects.filter(email=email.strip().lower()).first()

        if not temp_user:
            # If temp user does not exist, let's see if they are already fully onboarded in CustomUser!
            email_check = email.strip().lower() if email else None
            if email_check and User.objects.filter(email=email_check).exists():
                return self.error(
                    message="User is already fully registered and onboarded. Please log in.",
                    errors=["USER_ALREADY_ONBOARDED"],
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            return self.error(
                message="No active registration session found. Please sign up first.",
                errors=["SESSION_NOT_FOUND"],
                status_code=status.HTTP_404_NOT_FOUND
            )

        # Enforce password validation to prevent unauthorized session resumption
        from django.contrib.auth.hashers import check_password
        if not check_password(password, temp_user.password_hash):
            return self.error(
                message="Incorrect password",
                errors=["INVALID_CREDENTIALS"],
                status_code=status.HTTP_400_BAD_REQUEST
            )

        # Case 1: Registration exists but email is not verified yet
        if not temp_user.is_email_verified:
            # Re-generate OTP code cleanly (bypassing cooldown for resume)
            otp_code = f"{random.randint(100000, 999999)}"
            temp_user.otp_code = otp_code
            temp_user.otp_created_at = timezone.now()
            temp_user.save()

            try:
                send_email_task.delay(
                    subject="Verify your Voicebot Registration",
                    message=f"Your verification code is: {otp_code}. It is valid for {settings.OTP_VALIDITY_MINUTES} minutes.",
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                    recipient_list=[temp_user.email],
                    fail_silently=False,
                    title="Verify Your Registration",
                    lead_text="You requested to resume your onboarding session. Please use the verification code below to verify your email and resume.",
                    otp_code=otp_code,
                    body_text=f"This code is valid for {settings.OTP_VALIDITY_MINUTES} minutes. If you did not make this request, you can safely ignore this email."
                )
            except Exception as e:
                print(f"OTP Email queuing failed/skipped: {e}. OTP: {otp_code}")

            return self.success(
                data={
                    "temp_registration_id": str(temp_user.id),
                    "email": temp_user.email,
                    "step": "OTP_VERIFICATION_REQUIRED"
                },
                message="Registration is pending email verification. A new OTP has been emailed.",
                status_code=status.HTTP_200_OK
            )

        # Case 2: Email is verified. Reissue Temporary Scoped Token and resume from where they left off!
        temp_token = generate_temp_token(temp_user)

        # Get their selected plan
        plan = temp_user.selected_plan
        billing_breakdown = {
            "base_price": str(plan.base_price) if plan else "0.00",
            "discount_amount": str(plan.discount_amount) if plan else "0.00",
            "net_base_price": str(plan.net_base_price) if plan else "0.00",
            "tax_percentage": str(plan.tax_percentage) if plan else "0.00",
            "tax_amount": str(plan.tax_amount) if plan else "0.00",
            "total_price": str(plan.total_price) if plan else "0.00",
            "currency": plan.currency if plan else "INR"
        }

        # Check if they have already saved a billing address
        billing_address = BillingAddress.objects.filter(temp_user=temp_user).first()
        billing_address_data = None
        if billing_address:
            billing_address_data = BillingAddressSerializer(billing_address).data

        # Check if they have an active pending order
        pending_transaction = Transaction.objects.filter(
            temp_user=temp_user, status=Transaction.TransactionStatusChoices.PENDING
        ).first()

        # Determine the next flow step dynamically based on their saved data!
        if not billing_address:
            step = "BILLING_ADDRESS_REQUIRED"
        elif not pending_transaction:
            step = "PROCEED_TO_PAYMENT"
        else:
            step = "PAYMENT_VERIFICATION_REQUIRED"

        return self.success(
            data={
                "temporary_token": temp_token,
                "email": temp_user.email,
                "step": step,
                "plan": {
                    "id": str(plan.id) if plan else None,
                    "name": plan.name if plan else None,
                    "code": plan.code if plan else None
                } if plan else None,
                "billing_breakdown": billing_breakdown,
                "billing_address": billing_address_data,
                "razorpay_order_id": pending_transaction.razorpay_order_id if pending_transaction else None
            },
            message="Session resumed successfully.",
            status_code=status.HTTP_200_OK
        )

