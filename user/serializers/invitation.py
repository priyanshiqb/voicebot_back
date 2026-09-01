from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from tenant.models.TenantInvitation import TenantInvitation
from tenant.models.TenantUserMapping import TenantUserMapping


class InvitationRefSerializer(serializers.ModelSerializer):
    """Lightweight representation for nested relationships."""

    class Meta:
        model = TenantInvitation
        fields = ("id", "email", "role", "status")
        read_only_fields = fields


class InvitationSerializer(serializers.ModelSerializer):
    """Full detail. Read-only throughout — invitations are created by the service.

    `token_hash` is absent from every representation and always will be. It is the
    stored half of a credential, and an endpoint that returns it turns a list of
    pending invitations into a list of live ways in.
    """

    status = serializers.CharField(read_only=True)
    invited_by_email = serializers.EmailField(
        source="invited_by.email", read_only=True, default=None
    )

    class Meta:
        model = TenantInvitation
        fields = (
            "id",
            "email",
            "role",
            "status",
            "invited_by_email",
            "expires_at",
            "accepted_at",
            "created_at",
        )
        read_only_fields = fields


class InvitationListSerializer(serializers.ModelSerializer):
    """Optimised for the team-management table."""

    status = serializers.CharField(read_only=True)

    class Meta:
        model = TenantInvitation
        fields = ("id", "email", "role", "status", "expires_at", "created_at")
        read_only_fields = fields


class InvitationDropdownSerializer(serializers.ModelSerializer):
    label = serializers.CharField(source="email", read_only=True)
    value = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = TenantInvitation
        fields = ("label", "value")
        read_only_fields = fields


class InvitationCreateSerializer(serializers.Serializer):
    """What the inviter supplies: an address and a role. Never a password.

    The invitee chooses their own credentials at acceptance. That is the whole point —
    an admin who can set a colleague's password can become that colleague.
    """

    email = serializers.EmailField()
    role = serializers.ChoiceField(
        choices=[
            TenantUserMapping.Role.ADMIN,
            TenantUserMapping.Role.MEMBER,
        ],
        default=TenantUserMapping.Role.MEMBER,
    )


class InvitationAcceptSerializer(serializers.Serializer):
    """The unauthenticated half: prove you have the token, then choose a password."""

    token = serializers.CharField(write_only=True, trim_whitespace=True)
    password = serializers.CharField(
        write_only=True,
        validators=[validate_password],
        style={"input_type": "password"},
    )
    confirm_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )

    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError(
                {"confirm_password": "Passwords do not match."}
            )
        return attrs
