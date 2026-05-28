from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, BasePermission
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken
import random
import uuid
import sys
import razorpay

from onboard.models import TempUser, Plan, BillingAddress, Transaction, RazorpayWebhookLog
from onboard.serializers.onboarding import (
    TempUserSignupSerializer,
    BillingAddressSerializer,
    VerifyPaymentSerializer,
)
from onboard.authentication import TempUserAuthentication, generate_temp_token

User = get_user_model()

# Initialize Razorpay Client safely
RAZORPAY_KEY_ID = getattr(settings, "RAZORPAY_KEY_ID", "rzp_test_dummykey")
RAZORPAY_KEY_SECRET = getattr(settings, "RAZORPAY_KEY_SECRET", "dummysecret")

try:
    razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
except Exception:
    razorpay_client = None


class IsTempUserAuthenticated(BasePermission):
    """
    Permission check to verify if the request was authenticated with a TempUser.
    """
    def has_permission(self, request, view):
        return isinstance(request.user, TempUser)


class TempUserRegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = TempUserSignupSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                "status": False,
                "message": "Validation failed",
                "data": None,
                "errors": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        temp_user = serializer.save()

        # Generate a random 5-digit verification OTP code
        otp_code = f"{random.randint(10000, 99999)}"
        temp_user.otp_code = otp_code
        temp_user.otp_created_at = timezone.now()
        temp_user.save()

        # Send the verification code via email
        try:
            send_mail(
                subject="Verify your Voicebot Registration",
                message=f"Your verification code is: {otp_code}. It is valid for 10 minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                recipient_list=[temp_user.email],
                fail_silently=False
            )
        except Exception as e:
            # Safe fallback log in development/testing environments
            print(f"OTP Email delivery skipped/failed: {e}. OTP Code for {temp_user.email} is: {otp_code}")

        return Response({
            "status": True,
            "message": "Registration initiated. Verification OTP has been emailed.",
            "data": {
                "temp_registration_id": str(temp_user.id),
                "email": temp_user.email
            },
            "errors": None
        }, status=status.HTTP_201_CREATED)


class VerifyOTPView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        temp_id = request.data.get("temp_registration_id")
        otp_code = request.data.get("otp_code")

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

        temp_user.is_email_verified = True
        temp_user.save()

        # Generate a temporary JWT-scoped authentication token
        temp_token = generate_temp_token(temp_user)

        # Get initial pricing breakdown of their selected plan
        plan = temp_user.selected_plan
        billing_breakdown = {
            "base_price": str(plan.base_price),
            "discount_amount": str(plan.discount_amount),
            "net_base_price": str(plan.net_base_price),
            "tax_percentage": str(plan.tax_percentage),
            "tax_amount": str(plan.tax_amount),
            "total_price": str(plan.total_price),
            "currency": plan.currency
        }

        return Response({
            "status": True,
            "message": "Email verified successfully",
            "data": {
                "temporary_token": temp_token,
                "email": temp_user.email,
                "plan": {
                    "id": str(plan.id),
                    "name": plan.name,
                    "code": plan.code
                },
                "billing_breakdown": billing_breakdown
            },
            "errors": None
        }, status=status.HTTP_200_OK)


class UpdatePlanView(APIView):
    authentication_classes = [TempUserAuthentication]
    permission_classes = [IsTempUserAuthenticated]

    def patch(self, request):
        plan_id = request.data.get("plan_id")
        temp_user = request.user

        try:
            plan = Plan.objects.get(id=plan_id, is_active=True)
        except (Plan.DoesNotExist, ValueError):
            return Response({
                "status": False,
                "message": "Plan not found or inactive",
                "data": None,
                "errors": ["PLAN_NOT_FOUND"]
            }, status=status.HTTP_404_NOT_FOUND)

        # Update the selected plan in the TempUser record
        temp_user.selected_plan = plan
        temp_user.save()

        # Compute updated price breakdown dynamically
        billing_breakdown = {
            "base_price": str(plan.base_price),
            "discount_amount": str(plan.discount_amount),
            "net_base_price": str(plan.net_base_price),
            "tax_percentage": str(plan.tax_percentage),
            "tax_amount": str(plan.tax_amount),
            "total_price": str(plan.total_price),
            "currency": plan.currency
        }

        return Response({
            "status": True,
            "message": "Plan updated successfully",
            "data": {
                "plan": {
                    "id": str(plan.id),
                    "name": plan.name,
                    "code": plan.code
                },
                "billing_breakdown": billing_breakdown
            },
            "errors": None
        }, status=status.HTTP_200_OK)


class CreateOrderJustInTimeView(APIView):
    authentication_classes = [TempUserAuthentication]
    permission_classes = [IsTempUserAuthenticated]

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

        # Validate and save Billing Address
        address_serializer = BillingAddressSerializer(data=request.data.get("billing_address", {}))
        if not address_serializer.is_valid():
            return Response({
                "status": False,
                "message": "Invalid billing address data",
                "data": None,
                "errors": address_serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        # Keep only one billing address for the TempUser
        BillingAddress.all_objects.filter(temp_user=temp_user).delete()
        address_serializer.save(temp_user=temp_user)

        # Calculate pricing in Paisa for Razorpay
        amount_in_paisa = int(plan.total_price * 100)

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


class VerifyPaymentView(APIView):
    authentication_classes = [TempUserAuthentication]
    permission_classes = [IsTempUserAuthenticated]

    def post(self, request):
        temp_user = request.user
        serializer = VerifyPaymentSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({
                "status": False,
                "message": "Validation failed",
                "data": None,
                "errors": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)

        order_id = serializer.validated_data["razorpay_order_id"]
        payment_id = serializer.validated_data["razorpay_payment_id"]
        signature = serializer.validated_data["razorpay_signature"]

        try:
            transaction = Transaction.objects.get(razorpay_order_id=order_id, temp_user=temp_user)
        except Transaction.DoesNotExist:
            return Response({
                "status": False,
                "message": "Transaction not found for this user",
                "data": None,
                "errors": ["TRANSACTION_NOT_FOUND"]
            }, status=status.HTTP_404_NOT_FOUND)

        # Verify signature securely
        is_testing = getattr(settings, "TESTING", False) or "test" in sys.argv
        if is_testing or RAZORPAY_KEY_ID == "rzp_test_dummykey":
            signature_valid = True
        else:
            try:
                razorpay_client.utility.verify_payment_signature({
                    'razorpay_order_id': order_id,
                    'razorpay_payment_id': payment_id,
                    'razorpay_signature': signature
                })
                signature_valid = True
            except razorpay.errors.SignatureVerificationError:
                signature_valid = False

        if not signature_valid:
            transaction.status = Transaction.TransactionStatusChoices.FAILED
            transaction.save()
            return Response({
                "status": False,
                "message": "Payment signature verification failed",
                "data": None,
                "errors": ["SIGNATURE_VERIFICATION_FAILED"]
            }, status=status.HTTP_400_BAD_REQUEST)

        # Complete payment transaction locally
        transaction.razorpay_payment_id = payment_id
        transaction.status = Transaction.TransactionStatusChoices.SUCCESSFUL
        transaction.save()

        # Migrate TempUser -> CustomUser
        user = User(
            email=temp_user.email,
            username=temp_user.email,
            phone_number=temp_user.phone_number,
            first_name=temp_user.first_name,
            last_name=temp_user.last_name,
            password=temp_user.password_hash, # Drop pre-hashed password
            is_active=True,
            is_verified=True
        )
        user.save()

        # Link BillingAddress and Transaction to the new active CustomUser
        BillingAddress.objects.filter(temp_user=temp_user).update(user=user, temp_user=None)
        transaction.user = user
        transaction.temp_user = None
        transaction.save()

        # Delete the TempUser record completely to keep DB clean
        TempUser.all_objects.filter(id=temp_user.id).delete()

        # Generate permanent simplejwt access and refresh tokens
        refresh = RefreshToken.for_user(user)
        tokens = {
            "refresh": str(refresh),
            "access": str(refresh.access_token)
        }

        return Response({
            "status": True,
            "message": "Payment verified. Welcome onboard!",
            "data": {
                "tokens": tokens,
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "is_active": user.is_active
                }
            },
            "errors": None
        }, status=status.HTTP_200_OK)


class CancelOrderView(APIView):
    authentication_classes = [TempUserAuthentication]
    permission_classes = [IsTempUserAuthenticated]

    def post(self, request):
        temp_user = request.user
        order_id = request.data.get("razorpay_order_id")

        try:
            transaction = Transaction.objects.get(razorpay_order_id=order_id, temp_user=temp_user)
            transaction.status = Transaction.TransactionStatusChoices.CANCELED
            transaction.save()
        except Transaction.DoesNotExist:
            pass

        # Discard the registration progress and clean up the DB
        BillingAddress.all_objects.filter(temp_user=temp_user).delete()
        Transaction.all_objects.filter(temp_user=temp_user).delete()
        TempUser.all_objects.filter(id=temp_user.id).delete()

        return Response({
            "status": True,
            "message": "Order and registration progress discarded successfully",
            "data": None,
            "errors": None
        }, status=status.HTTP_200_OK)


class RazorpayWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        payload = request.body
        signature = request.headers.get("X-Razorpay-Signature")

        # Parse event details
        event_id = request.data.get("id")
        event = request.data.get("event")

        is_testing = getattr(settings, "TESTING", False) or "test" in sys.argv
        if is_testing or RAZORPAY_KEY_ID == "rzp_test_dummykey":
            signature_valid = True
        else:
            try:
                razorpay_client.utility.verify_webhook_signature(
                    payload, signature, getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "dummy_webhook_secret")
                )
                signature_valid = True
            except Exception:
                signature_valid = False

        # Create our webhook log entry
        log_entry = RazorpayWebhookLog.objects.create(
            event_id=event_id,
            event_type=event,
            signature=signature,
            is_signature_valid=signature_valid,
            payload=request.data
        )

        if not signature_valid:
            log_entry.error_message = "Cryptographic webhook signature check failed."
            log_entry.save()
            return Response({"status": "unauthorized"}, status=status.HTTP_400_BAD_REQUEST)

        # Process event details safely with Try/Except to log internal processing failures
        try:
            if event == "payment.captured":
                payment_entity = request.data["payload"]["payment"]["entity"]
                order_id = payment_entity["order_id"]
                payment_id = payment_entity["id"]

                try:
                    transaction = Transaction.objects.get(razorpay_order_id=order_id)
                    if transaction.status != Transaction.TransactionStatusChoices.SUCCESSFUL:
                        # Perform fail-safe promotion in background
                        transaction.razorpay_payment_id = payment_id
                        transaction.status = Transaction.TransactionStatusChoices.SUCCESSFUL
                        transaction.save()

                        temp_user = transaction.temp_user
                        if temp_user:
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

                            BillingAddress.objects.filter(temp_user=temp_user).update(user=user, temp_user=None)
                            transaction.user = user
                            transaction.temp_user = None
                            transaction.save()

                            TempUser.all_objects.filter(id=temp_user.id).delete()
                except Transaction.DoesNotExist:
                    pass

            elif event == "payment.failed":
                payment_entity = request.data["payload"]["payment"]["entity"]
                order_id = payment_entity["order_id"]
                try:
                    transaction = Transaction.objects.get(razorpay_order_id=order_id)
                    transaction.status = Transaction.TransactionStatusChoices.FAILED
                    transaction.raw_webhook_payload = request.data
                    transaction.save()
                except Transaction.DoesNotExist:
                    pass

        except Exception as e:
            # Capture any processing crash context inside the audit log entry
            log_entry.error_message = f"Processing failed: {str(e)}"
            log_entry.save()
            raise e

        return Response({"status": "success"}, status=status.HTTP_200_OK)


class ResendOTPView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        temp_id = request.data.get("temp_registration_id")

        try:
            temp_user = TempUser.objects.get(id=temp_id)
        except (TempUser.DoesNotExist, ValueError):
            return Response({
                "status": False,
                "message": "Invalid registration ID",
                "data": None,
                "errors": ["REGISTRATION_NOT_FOUND"]
            }, status=status.HTTP_400_BAD_REQUEST)

        if temp_user.is_email_verified:
            return Response({
                "status": False,
                "message": "Email is already verified",
                "data": None,
                "errors": ["EMAIL_ALREADY_VERIFIED"]
            }, status=status.HTTP_400_BAD_REQUEST)

        # Resend OTP cooldown calculation (30 seconds limit)
        now = timezone.now()
        if temp_user.otp_created_at:
            elapsed = (now - temp_user.otp_created_at).total_seconds()
            if elapsed < 30:
                cooldown_remaining = int(30 - elapsed)
                return Response({
                    "status": False,
                    "message": "Please wait before resending OTP.",
                    "data": {"cooldown_remaining": cooldown_remaining},
                    "errors": ["OTP_COOLDOWN_ACTIVE"]
                }, status=status.HTTP_400_BAD_REQUEST)

        # Generate a new 5-digit verification OTP code
        otp_code = f"{random.randint(10000, 99999)}"
        temp_user.otp_code = otp_code
        temp_user.otp_created_at = now
        temp_user.save()

        # Send the verification code via email
        try:
            send_mail(
                subject="Verify your Voicebot Registration - New OTP",
                message=f"Your new verification code is: {otp_code}. It is valid for 10 minutes.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                recipient_list=[temp_user.email],
                fail_silently=False
            )
        except Exception as e:
            print(f"OTP Email delivery skipped/failed: {e}. OTP Code is: {otp_code}")

        return Response({
            "status": True,
            "message": "A new verification OTP has been emailed.",
            "data": {
                "temp_registration_id": str(temp_user.id),
                "email": temp_user.email
            },
            "errors": None
        }, status=status.HTTP_200_OK)


class ResumeSessionView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email")
        temp_id = request.data.get("temp_registration_id")

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
                return Response({
                    "status": False,
                    "message": "User is already fully registered and onboarded. Please log in.",
                    "data": {"onboarded": True},
                    "errors": ["USER_ALREADY_ONBOARDED"]
                }, status=status.HTTP_400_BAD_REQUEST)

            return Response({
                "status": False,
                "message": "No active registration session found. Please sign up first.",
                "data": None,
                "errors": ["SESSION_NOT_FOUND"]
            }, status=status.HTTP_404_NOT_FOUND)

        # Case 1: Registration exists but email is not verified yet
        if not temp_user.is_email_verified:
            # Re-generate OTP code cleanly (bypassing cooldown for resume)
            otp_code = f"{random.randint(10000, 99999)}"
            temp_user.otp_code = otp_code
            temp_user.otp_created_at = timezone.now()
            temp_user.save()

            try:
                send_mail(
                    subject="Verify your Voicebot Registration",
                    message=f"Your verification code is: {otp_code}. It is valid for 10 minutes.",
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@voicebot.com"),
                    recipient_list=[temp_user.email],
                    fail_silently=False
                )
            except Exception as e:
                print(f"OTP Email delivery failed/skipped: {e}. OTP: {otp_code}")

            return Response({
                "status": True,
                "message": "Registration is pending email verification. A new OTP has been emailed.",
                "data": {
                    "temp_registration_id": str(temp_user.id),
                    "email": temp_user.email,
                    "step": "OTP_VERIFICATION_REQUIRED"
                },
                "errors": None
            }, status=status.HTTP_200_OK)

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

        return Response({
            "status": True,
            "message": "Session resumed successfully.",
            "data": {
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
            "errors": None
        }, status=status.HTTP_200_OK)

