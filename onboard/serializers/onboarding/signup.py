from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.validators import RegexValidator
from onboard.models import TempUser, Plan
from utils.emails import validate_not_disposable
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
    referral_code = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=50
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
    timezone_id = serializers.UUIDField(required=False, allow_null=True)
    language_id = serializers.UUIDField(required=False, allow_null=True)

    def validate_email(self, value):
        email_lower = value.strip().lower()
        # Before the uniqueness checks: a throwaway address is refused on its own
        # merits, and there is no reason to hit the database for one.
        validate_not_disposable(email_lower)
        if User.objects.filter(email=email_lower).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        if TempUser.objects.filter(email=email_lower).exists():
            raise serializers.ValidationError("A registration is already pending for this email.")
        return email_lower

    def validate_password(self, value):
        if " " in value:
            raise serializers.ValidationError("Password must not contain spaces.")
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

        try:
            plan = Plan.objects.get(id=attrs["plan_id"], is_active=True)
            attrs["plan_instance"] = plan
        except Plan.DoesNotExist:
            raise serializers.ValidationError({"plan_id": "Selected plan is invalid or inactive."})

        timezone_id = attrs.get("timezone_id")
        if timezone_id:
            from onboard.models.timezone import Timezone
            try:
                attrs["timezone_instance"] = Timezone.objects.get(id=timezone_id)
            except Timezone.DoesNotExist:
                raise serializers.ValidationError({"timezone_id": "Selected timezone is invalid."})

        language_id = attrs.get("language_id")
        if language_id:
            from onboard.models.language import Language
            try:
                attrs["language_instance"] = Language.objects.get(id=language_id)
            except Language.DoesNotExist:
                raise serializers.ValidationError({"language_id": "Selected language is invalid."})

        return attrs

    def create(self, validated_data):
        hashed_password = make_password(validated_data["password"])
        return TempUser.objects.create(
            email=validated_data["email"],
            referral_code=validated_data.get("referral_code"),
            password_hash=hashed_password,
            first_name=validated_data["first_name"],
            last_name=validated_data["last_name"],
            business_name=validated_data["business_name"],
            selected_plan=validated_data["plan_instance"],
            timezone=validated_data.get("timezone_instance"),
            language=validated_data.get("language_instance")
        )


class CheckEmailSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)


class CheckPhoneSerializer(serializers.Serializer):
    phone_number = serializers.CharField(required=True)
