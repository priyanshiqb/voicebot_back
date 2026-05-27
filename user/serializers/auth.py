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
        
        if not user.can_login_now():
            raise serializers.ValidationError({
                "login_window": [
                    "Login is not allowed at this time."
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