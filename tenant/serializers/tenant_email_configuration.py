from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from tenant.common import TenantBaseModelSerializer
from tenant.models.TenantEmailConfiguration import TenantEmailConfiguration
from utils.mail import providers, validators


# Changing any of these invalidates a previous successful test: different
# credentials, a different server, or a different sender identity is a different
# thing to have verified. Editing a label or toggling `is_active` is not.
_VERIFICATION_SENSITIVE_FIELDS = (
    "host",
    "port",
    "username",
    "password",
    "use_tls",
    "use_ssl",
    "from_email",
)


class TenantEmailConfigurationSerializer(TenantBaseModelSerializer):
    """
    A tenant's outbound mail credentials.

    Three rules shape this serializer, all of them about not handing a tenant a
    footgun:

    * ``password`` is write-only and never appears in a response, in any form —
      not even masked, since a fixed-length mask still leaks length. ``has_password``
      answers the only question a client actually needs.
    * ``is_default`` and every lifecycle field are read-only. The default moves
      through the ``set-default`` action, so a client cannot clear it and leave the
      tenant with none, nor set it on a second row and hit the unique index as an
      unhandled ``IntegrityError``.
    * Saving resets ``status`` to ``UNVERIFIED`` whenever the connection or sender
      changes, because treating an edited configuration as still-verified is how a
      broken one keeps being selected for real mail.

    The RESERVED columns on the model — API key, OAuth, domain delegation,
    display-name-only — are deliberately absent. They back integration approaches
    that do not exist yet, and exposing them would invite clients to populate
    fields nothing reads.
    """

    # Declared explicitly to lift the model's implicit `required=True`: a provider
    # preset may supply the host, so requiring it here would make picking SendGrid
    # from a dropdown pointless. Presence is enforced in validate() instead, where
    # the chosen provider is known.
    host = serializers.CharField(max_length=255, required=False, allow_blank=False)
    port = serializers.IntegerField(required=False, min_value=1, max_value=65535)
    use_tls = serializers.BooleanField(required=False)
    use_ssl = serializers.BooleanField(required=False)

    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        trim_whitespace=False,
        style={"input_type": "password"},
        help_text=(
            "SMTP password or API key. Write-only. Omit on update to keep the "
            "stored value."
        ),
    )
    has_password = serializers.SerializerMethodField()
    provider_label = serializers.SerializerMethodField()

    class Meta(TenantBaseModelSerializer.Meta):
        model = TenantEmailConfiguration
        fields = TenantBaseModelSerializer.Meta.fields + [
            "name",
            "provider",
            "provider_label",
            "is_default",
            "host",
            "port",
            "username",
            "password",
            "has_password",
            "use_tls",
            "use_ssl",
            "from_email",
            "from_name",
            "reply_to",
            "status",
            "verified_at",
            "last_tested_at",
            "last_used_at",
            "last_error",
            "is_active",
        ]
        read_only_fields = (
            *TenantBaseModelSerializer.Meta.read_only_fields,
            "provider_label",
            "has_password",
            "is_default",
            # Moves only through connect/disconnect, for the same reason as
            # is_default: a writable flag lets a client deactivate the default via
            # PATCH and skip the promotion that keeps a usable sender in place.
            "is_active",
            "status",
            "verified_at",
            "last_tested_at",
            "last_used_at",
            "last_error",
        )

    def get_has_password(self, obj):
        return obj.has_password()

    def get_provider_label(self, obj):
        return providers.get_preset(obj.provider)["label"]

    # --- validation ----------------------------------------------------------

    def _effective(self, attrs, field, fallback=None):
        """The value this field will hold after the save.

        A PATCH carries only what changed, so validating `attrs` alone would check
        a new port against a host that is not in the request.
        """
        if field in attrs:
            return attrs[field]
        if self.instance is not None:
            return getattr(self.instance, field)
        return fallback

    def _apply_preset(self, attrs):
        """Fill connection fields the client did not supply from the provider preset.

        Applied on create, and on update only when the provider itself changes —
        otherwise editing a name would quietly reset a host the tenant had
        deliberately customised.

        Values present in the request always win. A tenant on SENDGRID who sends
        `port: 2525` gets 2525; the preset fills gaps, it does not overwrite intent.
        """
        creating = self.instance is None
        provider_changed = (
            not creating
            and "provider" in attrs
            and attrs["provider"] != self.instance.provider
        )
        if not creating and not provider_changed:
            return

        provider = self._effective(
            attrs, "provider", TenantEmailConfiguration.Provider.CUSTOM
        )
        preset = providers.get_preset(provider)
        for field in providers.CONNECTION_FIELDS:
            if field not in attrs and preset.get(field) is not None:
                attrs[field] = preset[field]

    def validate_name(self, value):
        """Enforce the partial unique index in the serializer so it reads as a
        field error rather than surfacing as an IntegrityError."""
        value = (value or "").strip()
        if not value:
            return value
        tenant_id = getattr(self.instance, "tenant_id", None) or self._tenant_id()
        if tenant_id is None:
            return value
        clash = TenantEmailConfiguration.objects.filter(
            tenant_id=tenant_id, name__iexact=value
        )
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                "A configuration with this name already exists."
            )
        return value

    def _tenant_id(self):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        mapping = getattr(user, "tenant_mapping", None)
        return getattr(mapping, "tenant_id", None)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        self._apply_preset(attrs)

        provider = self._effective(
            attrs, "provider", TenantEmailConfiguration.Provider.CUSTOM
        )
        host = (self._effective(attrs, "host") or "").strip()

        if not host:
            # Only reachable for providers whose preset supplies no host — today
            # SES (region-dependent) and CUSTOM.
            label = providers.get_preset(provider)["label"]
            raise serializers.ValidationError(
                {"host": f"A host is required for {label}."}
            )
        attrs["host"] = host

        try:
            validators.validate_connection(
                host=host,
                port=self._effective(attrs, "port", 587),
                use_tls=self._effective(attrs, "use_tls", True),
                use_ssl=self._effective(attrs, "use_ssl", False),
            )
        except DjangoValidationError as exc:
            # The validators serve the model and the send path too, so they raise
            # Django's ValidationError. DRF does not catch that inside validate(),
            # and an uncaught one becomes a 500 instead of a 400.
            raise serializers.ValidationError(
                getattr(exc, "message_dict", None) or exc.messages
            )

        return attrs

    # --- persistence ---------------------------------------------------------

    def create(self, validated_data):
        raw_password = validated_data.pop("password", "")
        instance = TenantEmailConfiguration(**validated_data)
        instance.set_password(raw_password)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        # A PATCH that omits `password` must not wipe it. Reaching set_password(None)
        # here would silently blank a working configuration — the single easiest bug
        # to ship in this feature.
        password_supplied = "password" in validated_data
        raw_password = validated_data.pop("password", None)

        changed = [
            field
            for field in _VERIFICATION_SENSITIVE_FIELDS
            if field in validated_data
            and getattr(instance, field, None) != validated_data[field]
        ]
        if password_supplied and raw_password:
            changed.append("password")

        for field, value in validated_data.items():
            setattr(instance, field, value)
        if password_supplied and raw_password:
            instance.set_password(raw_password)

        if changed:
            instance.status = TenantEmailConfiguration.Status.UNVERIFIED
            instance.verified_at = None
            instance.last_error = ""

        instance.save()
        return instance


class TenantEmailConfigurationListSerializer(serializers.ModelSerializer):
    """Compact row for the integrations list. Carries `is_default` and `status`
    because those are the two things the list exists to show."""

    provider_label = serializers.SerializerMethodField()
    has_password = serializers.SerializerMethodField()

    class Meta:
        model = TenantEmailConfiguration
        fields = (
            "id",
            "name",
            "provider",
            "provider_label",
            "is_default",
            "host",
            "port",
            "from_email",
            "from_name",
            "status",
            "has_password",
            "is_active",
            "verified_at",
            "last_tested_at",
            "created_at",
        )
        read_only_fields = fields

    def get_provider_label(self, obj):
        return providers.get_preset(obj.provider)["label"]

    def get_has_password(self, obj):
        return obj.has_password()


class TenantEmailConfigurationDropdownSerializer(serializers.Serializer):
    value = serializers.UUIDField(source="id")
    label = serializers.SerializerMethodField()

    def get_label(self, obj):
        return obj.name or f"{obj.host}:{obj.port}"
