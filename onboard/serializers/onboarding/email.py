from rest_framework import serializers


class ChangeEmailSerializer(serializers.Serializer):
    temp_registration_id = serializers.UUIDField(required=True)
    new_email = serializers.EmailField(required=True)

    def validate_new_email(self, value):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        value = value.lower().strip()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("User account with this email address already exists.")
        return value
