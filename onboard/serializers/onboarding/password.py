from rest_framework import serializers
import re


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)


class ResetPasswordSerializer(serializers.Serializer):
    temp_registration_id = serializers.UUIDField(required=True)
    token = serializers.CharField(required=True)
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

    def validate(self, data):
        if data["password"] != data["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
        return data


class ForgotPasswordVerifyOTPSerializer(serializers.Serializer):
    temp_registration_id = serializers.UUIDField(required=True)
    otp_code = serializers.CharField(max_length=6, min_length=5, required=True)


class ForgotPasswordResendOTPSerializer(serializers.Serializer):
    temp_registration_id = serializers.UUIDField(required=True)

