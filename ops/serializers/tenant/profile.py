"""Editing a tenant's own record — the three fields an operator may correct.

Deliberately not a `ModelSerializer` over the whole model. `subdomain` is a routing
key, `tenant_referral_code` is somebody else's referral history, and `is_active` is a
lifecycle transition with its own endpoint and its own audit code. A field is here
because somebody typed it wrong at signup, and for no other reason.
"""

from rest_framework import serializers

from tenant.models import Tenant


class TenantProfileWriteSerializer(serializers.ModelSerializer):
    """`PATCH` on name, timezone and language."""

    class Meta:
        model = Tenant
        fields = ("name", "timezone", "language")
        extra_kwargs = {
            "name": {"required": False, "allow_blank": False},
            # PROTECT foreign keys. DRF turns an unknown pk into a field error rather
            # than letting it reach the database as a 500, which is the whole reason
            # these are declared instead of written straight through.
            "timezone": {"required": False, "allow_null": True},
            "language": {"required": False, "allow_null": True},
        }

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Nothing to change. Send at least one of name, timezone or language."
            )
        return attrs


class EnforcementSerializer(serializers.Serializer):
    """The two switches that decide whether a limit actually stops anybody.

    Both optional, because the console flips one at a time — but at least one must be
    present, or the audit row would record an operator changing nothing.
    """

    credits = serializers.BooleanField(required=False)
    entitlements = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if "credits" not in attrs and "entitlements" not in attrs:
            raise serializers.ValidationError(
                "Send at least one of credits or entitlements."
            )
        return attrs
