from rest_framework import serializers

from core.models.directory_contact import DirectoryContact
from tenant.common.serializers.tenant_base_model_serializer import (
    TenantBaseModelSerializer,
)

DIRECTORY_CONTACT_FIELDS = [
    "name",
    "designation",
    "category",
    "category_label",
    "department",
    "email",
    "phone_number",
    "phone_e164",
    "is_active",
]


class DirectoryContactSerializer(TenantBaseModelSerializer):
    """
    Full directory entry.

    `phone_e164` is derived on the model and exposed read-only — accepting it from
    a client would let it drift from the raw number everything else displays.
    """

    category_label = serializers.CharField(
        source="get_category_display",
        read_only=True,
        help_text="Human-readable category, for display without a client-side map.",
    )

    class Meta(TenantBaseModelSerializer.Meta):
        model = DirectoryContact
        fields = TenantBaseModelSerializer.Meta.fields + DIRECTORY_CONTACT_FIELDS
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields + (
            "phone_e164",
            "category_label",
        )

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Name is required.")
        return value

    def validate_designation(self, value):
        return (value or "").strip()

    def validate_email(self, value):
        """Lower-cased so uniqueness is not defeated by capitalisation.

        The unique constraint is a plain column comparison, so `Jane@acme.com` and
        `jane@acme.com` are two different rows to Postgres and one address to every
        human. Normalising on the way in is what makes the constraint mean what it
        appears to mean.
        """
        return (value or "").strip().lower()

    def validate(self, attrs):
        """Reject a duplicate address with a field error instead of a 500.

        The database constraint is the real guarantee — it holds against races this
        check cannot see. This exists so the ordinary case surfaces as a 400 on
        `email` rather than an IntegrityError, which the global handler can only
        render as a generic server failure.
        """
        email = attrs.get("email", getattr(self.instance, "email", "") or "")
        if not email:
            return attrs

        tenant_id = getattr(self.instance, "tenant_id", None) or getattr(
            getattr(self.context.get("request"), "user", None), "tenant_mapping", None
        )
        tenant_id = getattr(tenant_id, "tenant_id", tenant_id)
        if tenant_id is None:
            return attrs

        clash = DirectoryContact.objects.filter(tenant_id=tenant_id, email=email)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {"email": "Another directory contact already uses this email address."}
            )
        return attrs


class DirectoryContactListSerializer(TenantBaseModelSerializer):
    """Compact row for the directory list.

    Deliberately not the full serializer: the list is the most-hit endpoint here
    and the audit block (`created_by`/`updated_by`/`deleted_by`, each a nested
    user) is three joins per row that nothing in the directory UI renders.
    """

    category_label = serializers.CharField(
        source="get_category_display", read_only=True
    )

    class Meta:
        model = DirectoryContact
        fields = (
            "id",
            "name",
            "designation",
            "category",
            "category_label",
            "department",
            "email",
            "phone_number",
            "phone_e164",
            "is_active",
            "created_at",
        )
        read_only_fields = fields


class DirectoryContactDropdownSerializer(serializers.ModelSerializer):
    """`label`/`value` pairs, matching the shape the other dropdown endpoints use."""

    label = serializers.SerializerMethodField()
    value = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = DirectoryContact
        fields = ("label", "value")

    def get_label(self, obj):
        return f"{obj.name} — {obj.designation}" if obj.designation else obj.name
