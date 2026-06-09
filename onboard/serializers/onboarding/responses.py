from rest_framework import serializers


class BaseEnvelopeSerializer(serializers.Serializer):
    status = serializers.BooleanField(required=True)
    message = serializers.CharField(required=True)
    errors = serializers.JSONField(required=False, allow_null=True)


# --- Register Pending ---
class RegisterPendingResponseDataSerializer(serializers.Serializer):
    temp_registration_id = serializers.UUIDField(required=True)
    email = serializers.EmailField(required=True)


class RegisterPendingResponseSerializer(BaseEnvelopeSerializer):
    data = RegisterPendingResponseDataSerializer(allow_null=True)


# --- Verify OTP ---
class PlanResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField(required=True)
    name = serializers.CharField(required=True)
    code = serializers.CharField(required=True)


class BillingBreakdownResponseSerializer(serializers.Serializer):
    base_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    net_base_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    tax_percentage = serializers.DecimalField(max_digits=5, decimal_places=2)
    tax_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField()


class VerifyOTPResponseDataSerializer(serializers.Serializer):
    temporary_token = serializers.CharField(required=True)
    email = serializers.EmailField(required=True)
    plan = PlanResponseSerializer(required=True)
    billing_breakdown = BillingBreakdownResponseSerializer(required=True)


class VerifyOTPResponseSerializer(BaseEnvelopeSerializer):
    data = VerifyOTPResponseDataSerializer(allow_null=True)


# --- Resume Session ---
class ResumeSessionResponseDataSerializer(serializers.Serializer):
    temp_registration_id = serializers.UUIDField(required=True)
    email = serializers.EmailField(required=True)
    phone_number = serializers.CharField(required=False, allow_null=True)
    step = serializers.CharField(required=True)
    temporary_token = serializers.CharField(required=False, allow_null=True)
    plan = PlanResponseSerializer(required=False, allow_null=True)
    billing_breakdown = BillingBreakdownResponseSerializer(required=False, allow_null=True)


class ResumeSessionResponseSerializer(BaseEnvelopeSerializer):
    data = ResumeSessionResponseDataSerializer(allow_null=True)


# --- Update Plan ---
class UpdatePlanResponseDataSerializer(serializers.Serializer):
    plan = PlanResponseSerializer(required=True)
    billing_breakdown = BillingBreakdownResponseSerializer(required=True)


class UpdatePlanResponseSerializer(BaseEnvelopeSerializer):
    data = UpdatePlanResponseDataSerializer(allow_null=True)


# --- Verify Payment ---
class TokenDetailResponseSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=True)
    access = serializers.CharField(required=True)


class UserDetailResponseSerializer(serializers.Serializer):
    id = serializers.UUIDField(required=True)
    email = serializers.EmailField(required=True)
    is_active = serializers.BooleanField(required=True)


# --- Create Order JIT ---
class CreateOrderJustInTimeResponseDataSerializer(serializers.Serializer):
    razorpay_order_id = serializers.CharField(required=False)
    amount = serializers.IntegerField(required=False, help_text="Amount in Paisa")
    currency = serializers.CharField(required=False)
    razorpay_key_id = serializers.CharField(required=False)
    is_free = serializers.BooleanField(required=False, default=False)
    tokens = TokenDetailResponseSerializer(required=False, allow_null=True)
    user = UserDetailResponseSerializer(required=False, allow_null=True)


class CreateOrderJustInTimeResponseSerializer(BaseEnvelopeSerializer):
    data = CreateOrderJustInTimeResponseDataSerializer(allow_null=True)


class VerifyPaymentResponseDataSerializer(serializers.Serializer):
    tokens = TokenDetailResponseSerializer(required=True)
    user = UserDetailResponseSerializer(required=True)


class VerifyPaymentResponseSerializer(BaseEnvelopeSerializer):
    data = VerifyPaymentResponseDataSerializer(allow_null=True)


# --- General / Cancel / Forgot / Reset / Webhook ---
class GeneralResponseSerializer(BaseEnvelopeSerializer):
    data = serializers.JSONField(required=False, allow_null=True)


# --- Check Email Uniqueness ---
class CheckEmailResponseDataSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    available = serializers.BooleanField(required=True)
    exists_in_active_users = serializers.BooleanField(required=True)
    exists_in_pending_registrations = serializers.BooleanField(required=True)


class CheckEmailResponseSerializer(BaseEnvelopeSerializer):
    data = CheckEmailResponseDataSerializer(allow_null=True)
