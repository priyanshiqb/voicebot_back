"""
Serializers for the runner-facing campaign endpoints.

The runner receives a whole campaign, builds its own call queue, dials, and reports
per-member outcomes back. These serializers are that contract's two halves.
"""

from rest_framework import serializers

from core.models.agent import Agent
from core.models.campaign import Campaign
from core.models.campaign_member import CampaignMember


class RunnerAgentSerializer(serializers.ModelSerializer):
    """
    The bot config, inline.

    Delivered with the campaign so the runner does not need a second authenticated call
    to /agents/ before it can dial. Deliberately flat — knowledge bases and probing
    questions are not nested, so this payload does not grow every time the agent model
    does.
    """

    class Meta:
        model = Agent
        fields = [
            "id",
            "bot_name",
            "persona_name",
            "languages",
            "tone",
            "voice_name",
            "transfer_destinations",
        ]
        read_only_fields = fields


class RunnerMemberSerializer(serializers.ModelSerializer):
    """
    One person to call.

    `id` is the handle the runner echoes back on every update — it never has to
    interpret it, only return it.
    """

    full_name = serializers.CharField(source="contact.full_name", read_only=True)

    class Meta:
        model = CampaignMember
        fields = ["id", "full_name", "phone_e164", "attempt_count", "status"]
        read_only_fields = fields


class RunnerCampaignContextSerializer(serializers.ModelSerializer):
    """The campaign itself. Members are paginated separately alongside this."""

    agent = RunnerAgentSerializer(read_only=True)
    tenant_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Campaign
        fields = [
            "id",
            "tenant_id",
            "name",
            "description",
            "status",
            "agent",
            "handoff_id",
            "scheduled_start_at",
            "started_at",
            "total_members",
            "pending_count",
            "suppressed_count",
        ]
        read_only_fields = fields


class RunnerMemberUpdateSerializer(serializers.Serializer):
    """
    One member outcome.

    `status` is restricted to the runner-owned states. PENDING, SUPPRESSED, and
    CANCELLED are ours — accepting them here would let the runner un-suppress somebody
    who opted out, which is the one thing this whole design exists to prevent.
    """

    member_id = serializers.UUIDField()
    status = serializers.ChoiceField(
        choices=[(s, s) for s in CampaignMember.RUNNER_STATUSES]
    )
    outcome = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    error_message = serializers.CharField(
        required=False, allow_blank=True, default=""
    )
    attempted_at = serializers.DateTimeField(required=False, allow_null=True)
    provider_call_id = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )


class RunnerMemberUpdateBatchSerializer(serializers.Serializer):
    """
    A batch of outcomes, applied independently.

    Partial success on purpose: one malformed row must not reject the good updates
    alongside it, and the runner should not have to unpick a whole batch to retry.
    """

    updates = serializers.ListField(
        child=RunnerMemberUpdateSerializer(), allow_empty=False, max_length=1000
    )
