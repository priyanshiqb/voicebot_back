# serializers/auth.py

from rest_framework import serializers
from django.contrib.auth import authenticate


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()

class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField()

    def validate(self, attrs):
        email = attrs.get("email")
        password = attrs.get("password")

        user = authenticate(
            email=email,
            password=password
        )

        if not user:
            raise serializers.ValidationError({
                "credentials": [
                    "Invalid email or password"
                ]
            })
        
        if not user.is_active:
            raise serializers.ValidationError({
                "account": [
                    "User account is disabled"
                ]
            })
        
        attrs["user"] = user
        return attrs

class LoginResponseSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()
    user = serializers.DictField()


class TokenRefreshResponseSerializer(serializers.Serializer):
    access = serializers.CharField()


import re

class UserForgotPasswordSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)


class UserForgotPasswordVerifyOTPSerializer(serializers.Serializer):
    user_id = serializers.UUIDField(required=True)
    otp_code = serializers.CharField(max_length=6, min_length=5, required=True)


class UserResetPasswordSerializer(serializers.Serializer):
    user_id = serializers.UUIDField(required=True)
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