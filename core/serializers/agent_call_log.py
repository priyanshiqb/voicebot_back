from rest_framework import serializers
from tenant.common.serializers.tenant_base_model_core_serializer import TenantBaseModelCoreSerializer
from core.models.agent_call_log import AgentCallLog
from utils.serializers.hybrid_related_field import HybridRelatedField
from core.serializers.agent import AgentRefSerializer
from core.models.agent import Agent
from core.serializers.agent_call_recording import AgentCallRecordingRefSerializer
from core.models.agent_call_recording import AgentCallRecording

class AgentCallLogRefSerializer(serializers.ModelSerializer):
    """Lightweight serializer for nested relationships."""
    class Meta:
        model = AgentCallLog
        fields = ["id", "provider_call_id", "caller_number", "started_at"]

class AgentCallLogSerializer(TenantBaseModelCoreSerializer):
    """Full detail and write serializer for AgentCallLog."""
    agent = HybridRelatedField(
        serializer=AgentRefSerializer,
        queryset=Agent.objects.all()
    )
    recording_file = HybridRelatedField(
        serializer=AgentCallRecordingRefSerializer,
        queryset=AgentCallRecording.objects.all(),
        required=False,
        allow_null=True
    )

    class Meta(TenantBaseModelCoreSerializer.Meta):
        model = AgentCallLog
        fields = TenantBaseModelCoreSerializer.Meta.fields + [
            "provider_call_id",
            "agent",
            "caller_number",
            "caller_name",
            "direction",
            "campaign_id",
            "duration_seconds",
            # What actually served this call, which is what it is billed at. The
            # agent's configured tier is only the fallback: a bot set to realtime may
            # have been served on the standard model, and the tenant should pay for
            # what ran. Without this field on the write path the runner has no way to
            # say so, and the fallback becomes the only answer there is.
            "service_tier",
            "outcome",
            "mood",
            "mood_confidence",
            "pii_redacted",
            "ai_summary",
            "transcript",
            "recording_file",
            "started_at",
            "ended_at",
            "tags",
            "preferred_language",
            "probing_questions_count",
            "probing_questions",
            "follow_up_detail",
            "human_transfer_detail",
        ]

class AgentCallLogListSerializer(TenantBaseModelCoreSerializer):
    """Optimized serializer for list views."""
    agent = HybridRelatedField(
        serializer=AgentRefSerializer,
        read_only=True
    )

    class Meta(TenantBaseModelCoreSerializer.Meta):
        model = AgentCallLog
        fields = TenantBaseModelCoreSerializer.Meta.fields + [
            "provider_call_id",
            "agent",
            "mood",
            "caller_number",
            "direction",
            "duration_seconds",
            "service_tier",
            "outcome",
            "started_at"
        ]

class AgentCallLogDropdownSerializer(serializers.ModelSerializer):
    """Dropdown serializer for quick selection."""
    label = serializers.CharField(source="provider_call_id")
    value = serializers.UUIDField(source="id")

    class Meta:
        model = AgentCallLog
        fields = ["label", "value"]
