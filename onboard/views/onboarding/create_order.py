from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings
import uuid
import sys

from onboard.models import BillingAddress, Transaction
from onboard.serializers.onboarding import (
    BillingAddressSerializer,
    CreateOrderJustInTimeSerializer,
    CreateOrderJustInTimeResponseSerializer,
)
from onboard.authentication import TempUserAuthentication
from onboard.views.onboarding.base import (
    IsTempUserAuthenticated,
    RAZORPAY_KEY_ID,
    razorpay_client,
)
from drf_spectacular.utils import extend_schema
from user.serializers.custom_user import UserRefSerializer


@extend_schema(
    tags=["Onboarding - Signup Flow"],
    summary="Step 4: Create Razorpay Order Just-In-Time",
    description="Captures the billing address, validates the GSTIN number (if provided), and generates a new Razorpay payment gateway order.",
    responses={200: CreateOrderJustInTimeResponseSerializer}
)
class CreateOrderJustInTimeView(APIView):
    authentication_classes = [TempUserAuthentication]
    permission_classes = [IsTempUserAuthenticated]
    serializer_class = CreateOrderJustInTimeSerializer

    def post(self, request):
        temp_user = request.user
        plan = temp_user.selected_plan

        if not plan:
            return Response({
                "status": False,
                "message": "No plan selected",
                "data": None,
                "errors": ["PLAN_SELECTION_REQUIRED"]
            }, status=status.HTTP_400_BAD_REQUEST)

        # Validate and save Billing Address and phone_number
        serializer = CreateOrderJustInTimeSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                "status": False,
                "message": "Validation failed",
                "data": None,
                "errors": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        phone_number = serializer.validated_data["phone_number"]
        billing_address_data = serializer.validated_data["billing_address"]

        # Update phone number on TempUser
        temp_user.phone_number = phone_number
        temp_user.save()

        # Keep only one billing address for the TempUser
        BillingAddress.all_objects.filter(temp_user=temp_user).delete()
        address_serializer = BillingAddressSerializer(data=billing_address_data)
        address_serializer.is_valid(raise_exception=True)
        address_serializer.save(temp_user=temp_user)

        # Calculate pricing in Paisa for Razorpay
        amount_in_paisa = int(plan.total_price * 100)

        # Handle 0 amount (free trial/tier) checkouts by skipping Razorpay completely
        if amount_in_paisa == 0:
            # Save local successful transaction record
            transaction = Transaction.objects.create(
                temp_user=temp_user,
                plan=plan,
                razorpay_order_id=f"free_{uuid.uuid4().hex[:14]}",
                amount=plan.total_price,
                status=Transaction.TransactionStatusChoices.SUCCESSFUL
            )

            # Migrate TempUser -> CustomUser
            from django.contrib.auth import get_user_model
            from rest_framework_simplejwt.tokens import RefreshToken
            from onboard.views.onboarding.base import create_tenant_and_mapping
            
            User = get_user_model()
            user = User(
                email=temp_user.email,
                username=temp_user.email,
                phone_number=temp_user.phone_number,
                first_name=temp_user.first_name,
                last_name=temp_user.last_name,
                password=temp_user.password_hash,
                is_active=True,
                is_verified=True
            )
            user.save()

            # Create Tenant and Tenant User Mapping cleanly
            tenant = create_tenant_and_mapping(temp_user, user)

            # Create Subscription record for the new tenant with a plan snapshot
            from onboard.models import Subscription
            Subscription.objects.create(
                tenant=tenant,
                plan=plan,
                status=Subscription.SubscriptionStatusChoices.ACTIVE
            )

            # Link BillingAddress and Transaction to the new active CustomUser
            BillingAddress.objects.filter(temp_user=temp_user).update(user=user, temp_user=None)
            transaction.user = user
            transaction.temp_user = None
            transaction.save()

            # Mark the TempUser as onboarded and soft-delete
            temp_user.is_onboarded = True
            temp_user.save()
            temp_user.soft_delete()

            # Generate permanent simplejwt access and refresh tokens
            refresh = RefreshToken.for_user(user)
            access = refresh.access_token

            return Response({
                "status": True,
                "message": "Free onboarding completed successfully!",
                "data": {
                    "is_free": True,
                    "access": str(access),
                    "refresh": str(refresh),
                    "user": UserRefSerializer(user).data
                },
                "errors": None
            }, status=status.HTTP_200_OK)

        # Handle testing / offline fallback gracefully
        is_testing = getattr(settings, "TESTING", False) or "test" in sys.argv
        if is_testing or not RAZORPAY_KEY_ID or RAZORPAY_KEY_ID == "rzp_test_dummykey":
            razorpay_order_id = f"order_{uuid.uuid4().hex[:14]}"
        else:
            try:
                razorpay_order = razorpay_client.order.create({
                    "amount": amount_in_paisa,
                    "currency": "INR",
                    "receipt": f"receipt_{plan.code}",
                    "payment_capture": 1
                })
                razorpay_order_id = razorpay_order["id"]
            except Exception as e:
                return Response({
                    "status": False,
                    "message": f"Razorpay connection error: {str(e)}",
                    "data": None,
                    "errors": ["RAZORPAY_ERROR"]
                }, status=status.HTTP_502_BAD_GATEWAY)

        # Save Transaction record locally
        Transaction.objects.create(
            temp_user=temp_user,
            plan=plan,
            razorpay_order_id=razorpay_order_id,
            amount=plan.total_price,
            status=Transaction.TransactionStatusChoices.PENDING
        )

        return Response({
            "status": True,
            "message": "Razorpay order initiated",
            "data": {
                "razorpay_order_id": razorpay_order_id,
                "amount": amount_in_paisa,
                "currency": "INR",
                "razorpay_key_id": RAZORPAY_KEY_ID
            },
            "errors": None
        }, status=status.HTTP_200_OK)
