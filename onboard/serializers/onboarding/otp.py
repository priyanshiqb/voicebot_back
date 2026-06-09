from rest_framework import serializers


class VerifyOTPSerializer(serializers.Serializer):
    temp_registration_id = serializers.UUIDField(required=True)
    otp_code = serializers.CharField(required=True, min_length=6, max_length=6)


class ResendOTPSerializer(serializers.Serializer):
    temp_registration_id = serializers.UUIDField(required=True)
