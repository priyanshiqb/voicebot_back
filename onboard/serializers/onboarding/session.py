from rest_framework import serializers


class ResumeSessionSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    temp_registration_id = serializers.UUIDField(required=False, allow_null=True)
