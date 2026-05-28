from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.validators import RegexValidator
from onboard.models import TempUser, Plan, BillingAddress
import re

User = get_user_model()


class TempUserSignupSerializer(serializers.Serializer):
    plan_id = serializers.UUIDField(required=True)
    first_name = serializers.CharField(
        min_length=2,
        max_length=30,
        required=True,
        validators=[
            RegexValidator(
                regex=r"^[A-Za-z]+$",
                message="First name must contain only alphabetic characters."
            )
        ]
    )
    last_name = serializers.CharField(
        min_length=2,
        max_length=30,
        required=True,
        validators=[
            RegexValidator(
                regex=r"^[A-Za-z]+$",
                message="Last name must contain only alphabetic characters."
            )
        ]
    )
    business_name = serializers.CharField(
        min_length=2,
        max_length=255,
        required=True
    )
    email = serializers.EmailField(
        min_length=6,
        max_length=100,
        required=True
    )
    phone_number = serializers.CharField(
        required=True,
        validators=[
            RegexValidator(
                regex=r"^\d{7,15}$",
                message="Phone number must be between 7 and 15 digits and contain numbers only."
            )
        ]
    )
    password = serializers.CharField(
        min_length=8,
        max_length=32,
        required=True,
        style={"input_type": "password"}
    )
    confirm_password = serializers.CharField(
        required=True,
        style={"input_type": "password"}
    )

    def validate_email(self, value):
        email_lower = value.strip().lower()
        # Duplicate check in primary CustomUser table
        if User.objects.filter(email=email_lower).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        # Duplicate check in TempUser table
        if TempUser.objects.filter(email=email_lower).exists():
            raise serializers.ValidationError("A registration is already pending for this email.")
        return email_lower

    def validate_phone_number(self, value):
        # Duplicate check in primary CustomUser table
        if User.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError("A user with this phone number already exists.")
        # Duplicate check in TempUser table
        if TempUser.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError("A registration is already pending for this phone number.")
        return value

    def validate_password(self, value):
        if " " in value:
            raise serializers.ValidationError("Password must not contain spaces.")
        # Mixed case, numbers, special character check
        if not re.search(r"[A-Z]", value):
            raise serializers.ValidationError("Password must contain at least one uppercase letter.")
        if not re.search(r"[a-z]", value):
            raise serializers.ValidationError("Password must contain at least one lowercase letter.")
        if not re.search(r"\d", value):
            raise serializers.ValidationError("Password must contain at least one digit.")
        if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", value):
            raise serializers.ValidationError("Password must contain at least one special character.")
        return value

    def validate(self, attrs):
        if attrs.get("password") != attrs.get("confirm_password"):
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})

        # Validate that the selected Plan exists and is active
        try:
            plan = Plan.objects.get(id=attrs["plan_id"], is_active=True)
            attrs["plan_instance"] = plan
        except Plan.DoesNotExist:
            raise serializers.ValidationError({"plan_id": "Selected plan is invalid or inactive."})

        return attrs

    def create(self, validated_data):
        # Encrypt password securely
        hashed_password = make_password(validated_data["password"])

        return TempUser.objects.create(
            email=validated_data["email"],
            phone_number=validated_data["phone_number"],
            password_hash=hashed_password,
            first_name=validated_data["first_name"],
            last_name=validated_data["last_name"],
            business_name=validated_data["business_name"],
            selected_plan=validated_data["plan_instance"]
        )
class BillingAddressSerializer(serializers.ModelSerializer):
    gstin = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = BillingAddress
        fields = ["line1", "city", "state", "zip_code", "country", "gstin"]

    def validate_gstin(self, value):
        if not value:
            return value
        value = value.strip().upper()
        # Strictly check length
        if len(value) != 15:
            raise serializers.ValidationError("GSTIN must be exactly 15 characters long.")
            
        # Official Indian GSTIN format Regex validation
        gst_regex = r"^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}[Z]{1}[A-Z\d]{1}$"
        if not re.match(gst_regex, value):
            raise serializers.ValidationError("Please enter a valid GSTIN format (e.g. 07AAAAA1111A1Z1).")
        
        # State prefix code validation (01-38, or 97, 98, 99)
        try:
            state_code = int(value[:2])
        except ValueError:
            raise serializers.ValidationError("GSTIN state prefix must be numeric.")
            
        valid_state_codes = list(range(1, 39)) + [97, 98, 99]
        if state_code not in valid_state_codes:
            raise serializers.ValidationError("Invalid GSTIN state prefix code.")
            
        return value


class VerifyPaymentSerializer(serializers.Serializer):
    razorpay_order_id = serializers.CharField(required=True)
    razorpay_payment_id = serializers.CharField(required=True)
    razorpay_signature = serializers.CharField(required=True)
