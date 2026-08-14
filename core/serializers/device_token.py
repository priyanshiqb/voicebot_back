from rest_framework import serializers

from tenant.common import TenantBaseModelCoreSerializer
from core.models.device_token import DeviceToken


class DeviceTokenSerializer(TenantBaseModelCoreSerializer):
    class Meta(TenantBaseModelCoreSerializer.Meta):
        model = DeviceToken
        fields = (
            "id",
            "tenant",
            "user",
            "token",
            "platform",
            "is_active",
            "last_seen_at",
            "last_error",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = TenantBaseModelCoreSerializer.Meta.read_only_fields + (
            "user",
            "is_active",
            "last_seen_at",
            "last_error",
        )

    def validate_token(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("token is required.")
        return value.strip()


class DeviceTokenListSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceToken
        # `last_error` is exposed because "this device stopped getting alerts" was
        # otherwise unanswerable — is_active alone cannot distinguish a sign-out
        # from FCM rejecting the token from the staleness sweep aging it out.
        fields = (
            "id",
            "user",
            "platform",
            "is_active",
            "last_seen_at",
            "last_error",
            "created_at",
        )


class DeviceTokenDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="platform")
    value = serializers.UUIDField(source="id")


class DeviceTokenTestRequestSerializer(serializers.Serializer):
    token = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=(
            "Send to this exact FCM token, verbatim — no device lookup, no staleness "
            "check. Use it to test a token the app just produced, before or without "
            "registering it. Omit to fan out to every device registered to you."
        ),
    )


class DeviceTokenTestResultSerializer(serializers.Serializer):
    id = serializers.UUIDField(
        allow_null=True, help_text="Null when the token is not a device you registered."
    )
    platform = serializers.CharField(allow_null=True)
    last_seen_at = serializers.DateTimeField(allow_null=True)
    known_device = serializers.BooleanField(
        help_text=(
            "Whether this token is registered to you. False means the push was sent "
            "to the string you supplied but no device row exists — so it will never "
            "receive a real notification, only this test."
        )
    )
    accepted = serializers.BooleanField(
        help_text="FCM accepted the message for this device. Not proof of delivery."
    )
    detail = serializers.CharField(
        allow_null=True, help_text="Why it was not accepted, when it was not."
    )
    deactivated = serializers.BooleanField(
        help_text="The attempt retired this device — FCM reported it as gone."
    )


class DeviceTokenTestResponseSerializer(serializers.Serializer):
    push_enabled = serializers.BooleanField(
        help_text="Whether push is configured on this server at all."
    )
    configuration_error = serializers.CharField(
        allow_null=True,
        help_text=(
            "Set but unusable — why the Firebase credentials failed to load. Null "
            "when the credentials are fine."
        ),
    )
    registered = serializers.IntegerField(
        help_text="Your non-deleted devices in this tenant, including stale ones."
    )
    targeted = serializers.IntegerField(
        help_text="How many of those we actually attempted — active and recently re-registered."
    )
    accepted = serializers.IntegerField(help_text="How many FCM accepted.")
    results = DeviceTokenTestResultSerializer(many=True)
