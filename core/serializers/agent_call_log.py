from urllib.parse import urlsplit

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
    # The direction-aware "who was this call with", so a nested reference does not
    # have to know that caller_number means different people on inbound and outbound.
    customer_number = serializers.CharField(read_only=True)

    class Meta:
        model = AgentCallLog
        fields = [
            "id",
            "provider_call_id",
            "caller_number",
            "customer_number",
            "started_at",
        ]

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
    # Derived, never written: the runner sends the two raw numbers and this service
    # decides which of them is the person. Exposing it spares every client from
    # reimplementing that rule — and from getting it wrong on old outbound rows.
    customer_number = serializers.CharField(read_only=True)

    class Meta(TenantBaseModelCoreSerializer.Meta):
        model = AgentCallLog
        fields = TenantBaseModelCoreSerializer.Meta.fields + [
            "provider_call_id",
            "agent",
            "caller_number",
            # Optional on the write path on purpose: the runner adds it on its own
            # deploy schedule, and requiring it would 400 every call log until both
            # sides shipped together.
            "callee_number",
            "customer_number",
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
            # For when the SIP provider already hosts the recording: the runner passes
            # their link instead of uploading a copy through request-upload-url.
            "external_recording_url",
            "started_at",
            "ended_at",
            "tags",
            "preferred_language",
            "probing_questions_count",
            "probing_questions",
            "follow_up_detail",
            "human_transfer_detail",
        ]

    def validate_external_recording_url(self, value):
        """Accept a provider's recording link only if it is safe to show every user.

        Checked here rather than left to the model's validator, because DRF replaces a
        URLField's own URLValidator with its default one, which also allows ftp.

        Credentials in the link are refused: the URL is returned to everyone in the
        tenant, so `user:pass@` would publish the provider account's login — and
        browsers will not send embedded credentials for an audio source anyway, so the
        link would not play either.
        """
        if not value:
            # One "no link" value, so `external_recording_url__isnull` finds them all.
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https"):
            raise serializers.ValidationError("Enter an http or https link.")
        if parts.username or parts.password:
            raise serializers.ValidationError(
                "Remove the username and password from the link; it is shown to "
                "everyone in the tenant."
            )
        return value

class AgentCallLogListSerializer(TenantBaseModelCoreSerializer):
    """Optimized serializer for list views."""
    agent = HybridRelatedField(
        serializer=AgentRefSerializer,
        read_only=True
    )

    customer_number = serializers.CharField(read_only=True)

    class Meta(TenantBaseModelCoreSerializer.Meta):
        model = AgentCallLog
        fields = TenantBaseModelCoreSerializer.Meta.fields + [
            "provider_call_id",
            "agent",
            "mood",
            "caller_number",
            "callee_number",
            "customer_number",
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
