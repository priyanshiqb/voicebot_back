from rest_framework import serializers

from utils.emails import validate_not_disposable


class ChangeEmailSerializer(serializers.Serializer):
    temp_registration_id = serializers.UUIDField(required=True)
    new_email = serializers.EmailField(required=True)

    def validate_new_email(self, value):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        value = value.lower().strip()
        # Same rule as signup. Without it, the change-email step is a way to walk a
        # registration onto a throwaway address after passing the check at signup.
        validate_not_disposable(value)
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("User account with this email address already exists.")
        return value
