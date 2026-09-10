from rest_framework import serializers

from tenant.common import TenantBaseModelCoreSerializer
from core.models.user_event_skill import UserEventSkill
from utils.serializers.hybrid_related_field import HybridRelatedField
from user.serializers.custom_user import UserRefSerializer
from core.serializers.event_template import EventTemplateRefSerializer
from user.models import CustomUser
from core.models.event_template import EventTemplate


class UserEventSkillRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserEventSkill
        fields = ("id", "user", "event_template")
        read_only_fields = fields


class UserEventSkillSerializer(TenantBaseModelCoreSerializer):
    user = HybridRelatedField(
        serializer=UserRefSerializer,
        queryset=CustomUser.objects.all(),
    )
    event_template = HybridRelatedField(
        serializer=EventTemplateRefSerializer,
        queryset=EventTemplate.objects.all(),
    )

    class Meta(TenantBaseModelCoreSerializer.Meta):
        model = UserEventSkill
        fields = (
            "id",
            "tenant",
            "user",
            "event_template",
            "is_active",
            "created_at",
            "updated_at",
            "deleted_at",
        )
        read_only_fields = TenantBaseModelCoreSerializer.Meta.read_only_fields

    def validate(self, attrs):
        request = self.context.get("request")
        tenant = None
        if request and request.user and not request.user.is_superuser:
            try:
                tenant = request.user.tenant_mapping.tenant
            except AttributeError:
                raise serializers.ValidationError(
                    "User is not associated with an active tenant."
                )

        event_template = attrs.get("event_template") or getattr(
            self.instance, "event_template", None
        )
        user = attrs.get("user") or getattr(self.instance, "user", None)

        if tenant is not None:
            if event_template is not None and event_template.tenant_id != tenant.id:
                raise serializers.ValidationError(
                    {"event_template": "Event template does not belong to your tenant."}
                )
            if user is not None:
                user_tenant = getattr(getattr(user, "tenant_mapping", None), "tenant", None)
                if user_tenant is None or user_tenant.id != tenant.id:
                    raise serializers.ValidationError(
                        {"user": "User does not belong to your tenant."}
                    )
            
            # BUG-000923: an inactive template must not be selectable. The dropdown no
            # longer offers one, but the id can still be posted directly, and linking a
            # host to a template nobody can book is a silent dead end — the booking and
            # availability serializers both refuse it downstream.
            if event_template is not None and not event_template.is_active:
                raise serializers.ValidationError(
                    {"event_template": "This event template is inactive."}
                )

            # A host may hold several skills — that is the point of the table. Only the
            # exact (user, template) pair is refused, matching the DB constraint.
            if user is not None and event_template is not None:
                qs = UserEventSkill.objects.filter(
                    user=user,
                    event_template=event_template,
                    tenant=tenant,
                    is_deleted=False,
                )
                if self.instance:
                    qs = qs.exclude(pk=self.instance.pk)
                if qs.exists():
                    raise serializers.ValidationError(
                        "This user is already linked to this event template."
                    )
        return attrs


class UserEventSkillListSerializer(serializers.ModelSerializer):
    user = HybridRelatedField(
        serializer=UserRefSerializer,
        read_only=True,
    )
    event_template = HybridRelatedField(
        serializer=EventTemplateRefSerializer,
        read_only=True,
    )

    class Meta:
        model = UserEventSkill
        fields = ("id", "user", "event_template", "is_active", "created_at")


class UserEventSkillDropdownSerializer(serializers.Serializer):
    label = serializers.CharField(source="id")
    value = serializers.UUIDField(source="id")
