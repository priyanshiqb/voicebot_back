from rest_framework import serializers

from tenant.models.TenantEmailConfiguration import TenantEmailConfiguration
from tenant.models.TenantSettings import TenantSettings


class BookingEmailStatusSerializer(serializers.Serializer):
    """Whether booking email will actually be sent, and if not, why.

    A bare id tells a client nothing useful: the pointer can be set and the
    configuration behind it disconnected, or reset to UNVERIFIED by a credential
    edit weeks later. This answers the question the settings screen is really
    asking — *are booking emails going out right now?*
    """

    will_send = serializers.BooleanField()
    reason = serializers.CharField(allow_null=True)
    configuration_id = serializers.UUIDField(allow_null=True)
    from_email = serializers.CharField(allow_null=True)


class TenantSettingsSerializer(serializers.ModelSerializer):
    """
    Per-tenant configuration. One row per tenant; read and written as a singleton.

    `booking_email_configuration` accepts the id of one of **this tenant's**
    verified, connected email configurations, or `null` to stop sending booking
    email entirely.
    """

    booking_email_configuration = serializers.PrimaryKeyRelatedField(
        # Narrowed per-request in __init__ — the default queryset here would let a
        # caller name any tenant's configuration.
        queryset=TenantEmailConfiguration.objects.none(),
        allow_null=True,
        required=False,
        help_text=(
            "Which SMTP configuration carries booking email. Null means booking "
            "email is not sent."
        ),
    )
    # A method field, not the nested serializer bound with source="*": the status is
    # computed from a live lookup, and binding it to the instance would make DRF
    # look for `will_send` as a model attribute.
    booking_email = serializers.SerializerMethodField()

    class Meta:
        model = TenantSettings
        fields = (
            "id",
            "booking_email_configuration",
            "booking_email",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant_id = self._tenant_id()
        if tenant_id is not None:
            # Scoping the queryset is the isolation boundary, not a convenience:
            # PrimaryKeyRelatedField resolves whatever id it is given, so an
            # unscoped queryset would let a tenant route their customer mail
            # through a stranger's credentials by guessing a UUID.
            self.fields["booking_email_configuration"].queryset = (
                TenantEmailConfiguration.objects.filter(tenant_id=tenant_id)
            )

    def _tenant_id(self):
        request = self.context.get("request")
        mapping = getattr(getattr(request, "user", None), "tenant_mapping", None)
        return getattr(mapping, "tenant_id", None)

    def validate_booking_email_configuration(self, config):
        """Refuse a sender that cannot actually send.

        Assigning a disconnected or never-tested configuration would leave booking
        email silently not sending, which looks identical to having configured
        nothing — the exact confusion this endpoint exists to remove.
        """
        if config is None:
            return None
        if not config.is_active:
            raise serializers.ValidationError(
                "That configuration is disconnected. Reconnect it first."
            )
        if config.status != TenantEmailConfiguration.Status.VERIFIED:
            raise serializers.ValidationError(
                "That configuration has not passed a test send yet. Test it first, "
                "so booking email is not assigned to a sender that cannot deliver."
            )
        return config

    def get_booking_email(self, instance) -> dict:
        sender = TenantSettings.booking_sender(instance.tenant_id)
        if sender is not None:
            return {
                "will_send": True,
                "reason": None,
                "configuration_id": str(sender.id),
                "from_email": sender.from_email,
            }

        assigned_id = instance.booking_email_configuration_id
        if assigned_id is None:
            reason = (
                "No email configuration is assigned, so booking email is not being "
                "sent. Booking email never falls back to the platform sender."
            )
        else:
            # The pointer survives changes to what it points at. Naming the actual
            # state stops a tenant from staring at a populated field wondering why
            # nothing arrives.
            config = TenantEmailConfiguration.objects.filter(pk=assigned_id).first()
            if config is None:
                reason = "The assigned configuration no longer exists."
            elif not config.is_active:
                reason = "The assigned configuration is disconnected."
            else:
                reason = (
                    f"The assigned configuration is {config.status} — it has not "
                    "passed a test send since it was last changed."
                )
        return {
            "will_send": False,
            "reason": reason,
            "configuration_id": str(assigned_id) if assigned_id else None,
            "from_email": None,
        }
