from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from rest_framework import serializers

from core.models.contact import Contact
from tenant.common.serializers.tenant_base_model_serializer import (
    TenantBaseModelSerializer,
)
from utils.phone import PhoneNormalizationError, normalize_phone

CONTACT_FIELDS = [
    "first_name",
    "last_name",
    "full_name",
    "phone_number",
    "phone_e164",
    "phone_region",
    "email_address",
    "company",
    "tags",
    "notes",
    "source",
    "consent_status",
    "consent_source",
    "consent_at",
    "do_not_call",
    "opted_out_at",
    "last_contacted_at",
    "is_active",
]


def _integrity_error_detail(exc):
    """
    Turn a lost uniqueness race into the same field error the pre-check would have
    given. The pre-checks above are queries, so two simultaneous writes can still both
    pass them and let the constraint decide — that must not surface as a 500.
    """
    message = str(exc)
    if "unique_active_contact_name_tenant" in message:
        return {"first_name": "A contact with this name already exists."}
    if "unique_active_contact_phone_tenant" in message:
        return {"phone_number": "A contact with this phone number already exists."}
    raise exc


class ContactSerializer(TenantBaseModelSerializer):
    """
    Full contact representation.

    `phone_e164` and `phone_region` are derived on the model and exposed read-only —
    accepting them from a client would let the identity key drift from the raw number.
    """

    full_name = serializers.CharField(read_only=True)
    # Declared explicitly so blank entries reach validate_tags to be dropped, rather
    # than being rejected by the ArrayField child before cleanup can run.
    tags = serializers.ListField(
        child=serializers.CharField(max_length=50, allow_blank=True),
        required=False,
    )

    class Meta(TenantBaseModelSerializer.Meta):
        model = Contact
        fields = TenantBaseModelSerializer.Meta.fields + CONTACT_FIELDS
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "phone_e164",
            "phone_region",
            "full_name",
            "last_contacted_at",
        )

    def validate_first_name(self, value):
        # Stripped before the uniqueness check so " John" cannot slip past a name
        # already stored as "John". The DB constraint lower()s but cannot trim.
        return (value or "").strip()

    def validate_last_name(self, value):
        return (value or "").strip()

    def validate_phone_number(self, value):
        """
        Normalize up front so a bad number is a clean 400 on the right field rather
        than a ValidationError surfacing from Model.save().
        """
        try:
            normalize_phone(value)
        except PhoneNormalizationError as exc:
            raise serializers.ValidationError(str(exc))
        return value

    def validate_tags(self, value):
        max_tags = getattr(settings, "CONTACT_MAX_TAGS_PER_CONTACT", 25)
        if len(value) > max_tags:
            raise serializers.ValidationError(
                f"A contact may have at most {max_tags} tags."
            )

        cleaned = []
        for tag in value:
            tag = (tag or "").strip()
            if not tag:
                continue
            if len(tag) > 50:
                raise serializers.ValidationError("Each tag may be at most 50 characters.")
            if tag not in cleaned:
                cleaned.append(tag)
        return cleaned

    def _uniqueness_message(self):
        return "A contact with this phone number already exists."

    def _check_unique(self, phone_number, instance=None):
        """
        Pre-empt the partial unique constraint so the client gets a field error rather
        than a 500 from the IntegrityError. The commit path still handles the race.
        """
        try:
            e164, _ = normalize_phone(phone_number)
        except PhoneNormalizationError:
            return  # validate_phone_number already reported this

        tenant = getattr(instance, "tenant_id", None) or self._tenant_id()
        if tenant is None:
            return

        qs = Contact.objects.filter(tenant_id=tenant, phone_e164=e164)
        if instance is not None:
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                {"phone_number": self._uniqueness_message()}
            )

    def _check_unique_name(self, attrs, instance=None):
        """
        Enforce the per-tenant name rule before the DB does, so a duplicate is a field
        error instead of a 500 out of the partial unique index.

        Reads through to the instance for whichever half was not supplied: a PATCH that
        sends only `last_name` still has to be checked against the stored `first_name`.
        """
        first_name = attrs.get(
            "first_name", getattr(instance, "first_name", "") or ""
        ).strip()
        last_name = attrs.get(
            "last_name", getattr(instance, "last_name", "") or ""
        ).strip()

        # Matches the constraint's carve-out: a wholly unnamed contact is allowed, and
        # there may be many of them (every contact captured from an inbound call).
        if not first_name and not last_name:
            return

        tenant = getattr(instance, "tenant_id", None) or self._tenant_id()
        if tenant is None:
            return

        qs = Contact.objects.filter(
            tenant_id=tenant,
            first_name__iexact=first_name,
            last_name__iexact=last_name,
        )
        if instance is not None:
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                {"first_name": "A contact with this name already exists."}
            )

    def _tenant_id(self):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        try:
            return user.tenant_mapping.tenant_id
        except Exception:
            return None

    def validate(self, attrs):
        attrs = super().validate(attrs)
        phone_number = attrs.get("phone_number")
        if phone_number:
            self._check_unique(phone_number, instance=self.instance)
        if "first_name" in attrs or "last_name" in attrs:
            self._check_unique_name(attrs, instance=self.instance)
        return attrs

    def create(self, validated_data):
        try:
            return super().create(validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        except IntegrityError as exc:
            raise serializers.ValidationError(_integrity_error_detail(exc))

    def update(self, instance, validated_data):
        try:
            return super().update(instance, validated_data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        except IntegrityError as exc:
            raise serializers.ValidationError(_integrity_error_detail(exc))


class ContactListSerializer(TenantBaseModelSerializer):
    """Lighter payload for list views — skips notes and consent detail."""

    full_name = serializers.CharField(read_only=True)

    class Meta(TenantBaseModelSerializer.Meta):
        model = Contact
        fields = TenantBaseModelSerializer.Meta.fields + [
            "first_name",
            "last_name",
            "full_name",
            "phone_number",
            "phone_e164",
            "email_address",
            "company",
            "tags",
            "source",
            "consent_status",
            "do_not_call",
            "is_active",
        ]
        read_only_fields = fields


class ContactDropdownSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = Contact
        fields = ["label", "value"]

    def get_label(self, obj):
        name = obj.full_name
        return f"{name} ({obj.phone_e164})" if name else obj.phone_e164


class ContactLookupQuerySerializer(serializers.Serializer):
    """Query params for the inbound-caller lookup action."""

    phone = serializers.CharField(required=True)
    region = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        try:
            e164, _ = normalize_phone(attrs["phone"], region=attrs.get("region"))
        except PhoneNormalizationError as exc:
            raise serializers.ValidationError({"phone": str(exc)})
        attrs["phone_e164"] = e164
        return attrs
