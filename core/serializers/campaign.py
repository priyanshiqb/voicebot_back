from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.models.agent import Agent
from core.models.campaign import DEFAULT_SCHEDULE_TIMEZONE, Campaign
from core.models.campaign_member import CampaignMember
from core.models.contact import Contact
from core.serializers.agent import AgentRefSerializer
from onboard.models.timezone import validate_iana_timezone
from tenant.common.serializers.tenant_base_model_serializer import (
    TenantBaseModelSerializer,
)
from utils.serializers.hybrid_related_field import HybridRelatedField

# %d/%m/%Y must come first: DRF tries formats in order and "01/02/2027" parses under
# both day-first and month-first, so the wrong order is silently off by ten months.
# ISO is kept as a fallback because that is what a machine client sends.
DATE_INPUT_FORMATS = ["%d/%m/%Y", "%Y-%m-%d"]
TIME_INPUT_FORMATS = ["%H:%M", "%I:%M %p", "%I:%M%p", "%H:%M:%S"]

# Fields the campaign lifecycle derives. Never accepted from a client.
DERIVED_FIELDS = (
    "status",
    "started_at",
    "completed_at",
    "launched_by",
    "paused_by",
    "cancelled_by",
    "handoff_id",
    "handoff_status",
    "handed_off_at",
    "total_members",
    "pending_count",
    "suppressed_count",
    "completed_count",
    "failed_count",
    "error_message",
)

def tenant_from_context(context):
    """
    The caller's tenant, or None when there isn't one.

    Returns None rather than raising for an anonymous or unmapped user. Schema
    generation instantiates serializers with no real request, and raising there drops
    the entire viewset out of the OpenAPI document. A genuinely unmapped user is already
    stopped with a 403 by `TenantBaseViewSet` before any of this runs.
    """
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    try:
        return user.tenant_mapping.tenant
    except Exception:
        return None


class OffsetDateTimeField(serializers.DateTimeField):
    """
    An ISO-8601 instant that must say which offset it is in.

    DRF's DateTimeField quietly reads a timestamp with no offset as server time (UTC
    here). For a campaign start that turns "13:00, meaning India" into 13:00 UTC, five
    and a half hours late, with nothing in the response to show it. Refusing is the only
    safe reading of an instant that does not say where it is.
    """

    default_error_messages = {
        "naive": (
            "Include a UTC offset: end the time with Z (UTC) or an offset such as "
            "+05:30."
        ),
    }

    def enforce_timezone(self, value):
        if timezone.is_naive(value):
            self.fail("naive")
        return super().enforce_timezone(value)


class CampaignRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = ("id", "name", "status")


class CampaignSerializer(TenantBaseModelSerializer):
    """
    Full campaign representation and the only write path.

    Model validators are restated here because DRF never calls `full_clean()`. The one
    exception is the schedule: `Campaign.set_start_instant()` and the deprecated
    `Campaign.set_schedule()` already hold the future-start rule (and, for the date/time
    pickers, the timezone fallback), so this serializer calls them and translates the
    error rather than keeping a second copy of that logic.
    """

    agent = HybridRelatedField(
        serializer=AgentRefSerializer,
        queryset=Agent.objects.none(),
    )
    contacts = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Contact.objects.none(),
        required=False,
    )

    # The start, as the client resolved it. The preferred way to schedule.
    scheduled_start_at = OffsetDateTimeField(
        required=False,
        allow_null=True,
        help_text=(
            "When the campaign starts, as an ISO-8601 instant with its offset, e.g. "
            "2026-09-24T07:30:00Z or 2026-09-24T13:00:00+05:30. Stored as sent; no "
            "timezone is applied. null clears the schedule."
        ),
    )
    schedule_timezone = serializers.CharField(
        max_length=64,
        required=False,
        allow_blank=True,
        help_text=(
            "IANA zone this campaign's times are shown in, e.g. Asia/Kolkata: the start "
            "as typed, and every time in its report. Only a label when "
            "scheduled_start_at is sent; the zone start_date/start_time are read in "
            "when those are sent instead."
        ),
    )
    # Deprecated: kept so clients can move to scheduled_start_at on their own schedule.
    start_date = serializers.DateField(
        input_formats=DATE_INPUT_FORMATS,
        write_only=True,
        required=False,
        allow_null=True,
        help_text="Deprecated — send scheduled_start_at instead.",
    )
    start_time = serializers.TimeField(
        input_formats=TIME_INPUT_FORMATS,
        write_only=True,
        required=False,
        allow_null=True,
        help_text="Deprecated — send scheduled_start_at instead.",
    )
    start_local = serializers.SerializerMethodField()
    contact_count = serializers.SerializerMethodField()

    class Meta(TenantBaseModelSerializer.Meta):
        model = Campaign
        fields = TenantBaseModelSerializer.Meta.fields + [
            "name",
            "description",
            "agent",
            "status",
            "audience_source",
            "contacts",
            "contact_count",
            "start_date",
            "start_time",
            "start_local",
            "scheduled_start_at",
            "schedule_timezone",
            "started_at",
            "completed_at",
            "handoff_status",
            "handoff_id",
            "handed_off_at",
            "total_members",
            "pending_count",
            "suppressed_count",
            "completed_count",
            "failed_count",
            "error_message",
            "is_active",
        ]
        read_only_fields = (
            *TenantBaseModelSerializer.Meta.read_only_fields,
            *DERIVED_FIELDS,
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Scoping the querysets IS the cross-tenant guard. Another tenant's id then
        # resolves to "does not exist" as a matter of course, rather than depending on a
        # hand-written ownership check that can be removed without breaking a test.
        agents = Agent.objects.filter(is_deleted=False, is_active=True)
        contacts = Contact.objects.filter(is_deleted=False)

        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not getattr(user, "is_superuser", False):
            tenant = tenant_from_context(self.context)
            # No tenant means anonymous or schema generation. Empty, never unfiltered —
            # falling back to "all objects" here would be the cross-tenant hole itself.
            agents = agents.filter(tenant=tenant) if tenant else agents.none()
            contacts = contacts.filter(tenant=tenant) if tenant else contacts.none()

        self.fields["agent"].queryset = agents
        self.fields["contacts"].child_relation.queryset = contacts

    @extend_schema_field(
        {
            "type": "object",
            "nullable": True,
            "properties": {
                "date": {"type": "string", "format": "date"},
                "time": {"type": "string"},
                "timezone": {"type": "string"},
            },
        }
    )
    def get_start_local(self, obj):
        """The schedule as the user typed it, so the pickers repopulate correctly."""
        local = obj.local_start
        if local is None:
            return None
        return {
            "date": local.date().isoformat(),
            "time": local.strftime("%H:%M"),
            "timezone": obj.schedule_timezone or DEFAULT_SCHEDULE_TIMEZONE,
        }

    @extend_schema_field(serializers.IntegerField())
    def get_contact_count(self, obj):
        if obj.pk is None:
            return 0
        return obj.contacts.count()

    def validate_name(self, value):
        exclude_pk = self.instance.pk if self.instance is not None else None
        if Campaign.name_in_use(
            value, tenant=tenant_from_context(self.context), exclude_pk=exclude_pk
        ):
            raise serializers.ValidationError(
                "A campaign with this name already exists."
            )
        return value

    def validate_contacts(self, value):
        cap = getattr(settings, "CAMPAIGN_MAX_SELECTED_CONTACTS", 5_000)
        if len(value) > cap:
            raise serializers.ValidationError(
                f"A campaign cannot target more than {cap} contacts at once."
            )
        return value

    def validate_audience_source(self, value):
        if value != Campaign.AudienceSource.SELECTED:
            raise serializers.ValidationError(
                f"'{value}' audiences are not supported yet. Use "
                f"'{Campaign.AudienceSource.SELECTED}'."
            )
        return value

    def validate_schedule_timezone(self, value):
        if not value:
            return value
        try:
            validate_iana_timezone(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages[0])
        return value

    def validate(self, attrs):
        # The status-based edit guard lives in the viewset, not here. DRF's
        # run_validation catches whatever validate() raises and re-raises a plain
        # ValidationError, so a 409-carrying subclass cannot survive this method — the
        # response would always be a 400.
        attrs = super().validate(attrs)

        submitted = getattr(self, "initial_data", {}) or {}
        sends_instant = "scheduled_start_at" in submitted
        sends_pickers = "start_date" in submitted or "start_time" in submitted
        if sends_instant and sends_pickers:
            # Two starts in one request, possibly disagreeing. There is no right one to
            # pick, so refuse rather than silently prefer either.
            raise serializers.ValidationError(
                {
                    "scheduled_start_at": (
                        "Send scheduled_start_at, or start_date with start_time, "
                        "not both."
                    )
                }
            )

        # Popped so none reach Model(**validated_data) directly: the schedule goes
        # through the model's own methods, which hold the future-start rule.
        instant = attrs.pop("scheduled_start_at", None)
        start_date = attrs.pop("start_date", None)
        start_time = attrs.pop("start_time", None)
        if sends_instant:
            attrs["_schedule"] = {"instant": instant}
        elif sends_pickers:
            attrs["_schedule"] = {
                "date": start_date,
                "time": start_time,
                # The zone the pickers were filled in, when the client says so.
                # set_schedule() falls back to the tenant's, then UTC.
                "tz": attrs.get("schedule_timezone") or None,
            }

        return attrs

    def _apply_schedule(self, instance, schedule):
        if schedule is None:
            return
        try:
            if "instant" in schedule:
                instance.set_start_instant(schedule["instant"])
            else:
                instance.set_schedule(
                    schedule["date"], schedule["time"], tz_name=schedule["tz"]
                )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else str(exc)
            )

    def create(self, validated_data):
        contacts = validated_data.pop("contacts", [])
        schedule = validated_data.pop("_schedule", None)

        instance = Campaign(**validated_data)
        self._apply_schedule(instance, schedule)
        instance.save()
        instance.contacts.set(contacts)
        return instance

    def update(self, instance, validated_data):
        contacts = validated_data.pop("contacts", None)
        schedule = validated_data.pop("_schedule", None)

        for field, value in validated_data.items():
            setattr(instance, field, value)
        self._apply_schedule(instance, schedule)
        instance.save()

        if contacts is not None:
            instance.contacts.set(contacts)
        return instance


class CampaignListSerializer(TenantBaseModelSerializer):
    """
    Lighter payload for list views.

    `contacts` is omitted deliberately: an audience of thousands, across ten campaigns
    per page, is tens of thousands of rows fetched to render a list that does not show
    them. `total_members` answers the only question a list needs to.
    """

    agent = HybridRelatedField(serializer=AgentRefSerializer, read_only=True)

    class Meta(TenantBaseModelSerializer.Meta):
        model = Campaign
        fields = TenantBaseModelSerializer.Meta.fields + [
            "name",
            "description",
            "agent",
            "status",
            "audience_source",
            "scheduled_start_at",
            "schedule_timezone",
            "started_at",
            "completed_at",
            "handoff_status",
            "total_members",
            "pending_count",
            "suppressed_count",
            "completed_count",
            "failed_count",
            "is_active",
        ]
        read_only_fields = fields


class CampaignDropdownSerializer(serializers.ModelSerializer):
    label = serializers.CharField(source="name", read_only=True)
    value = serializers.UUIDField(source="id", read_only=True)

    class Meta:
        model = Campaign
        fields = ["label", "value"]


class CampaignMemberSerializer(serializers.ModelSerializer):
    """
    Audience review payload: enough to see who is in and who was refused.

    Contact detail is limited to name and number — the campaign's own audience, already
    visible through /contacts/ — rather than nesting the full contact and widening the
    PII surface of every campaign export.
    """

    contact_id = serializers.UUIDField(source="contact.id", read_only=True)
    full_name = serializers.CharField(source="contact.full_name", read_only=True)
    # The member's newest call log, i.e. the id GET /agent-call-logs/{id}/ opens. An
    # annotation the members action adds, so null both when nothing is linked and when
    # a caller serializes a member without it.
    analytics_id = serializers.UUIDField(read_only=True, allow_null=True)
    # Length of that same latest call, in seconds. Also an annotation; null when no call
    # is linked.
    duration_seconds = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = CampaignMember
        fields = [
            "id",
            "contact_id",
            "full_name",
            "phone_e164",
            "status",
            "suppression_reason",
            "attempt_count",
            "last_call_at",
            "outcome",
            "error_message",
            "analytics_id",
            "duration_seconds",
            "created_at",
        ]
        read_only_fields = fields


class CampaignMemberFilterSerializer(serializers.Serializer):
    """
    The query parameters of GET /campaigns/{id}/members/ that need checking.

    A duration that is not a whole number, or a range the wrong way round, would
    otherwise become a filter that silently matches nobody, and an empty audience reads
    as a real answer. A parameter sent empty (`?min_duration=`) counts as not sent.
    """

    min_duration = serializers.IntegerField(
        min_value=0,
        required=False,
        help_text="Latest call at least this many seconds long, inclusive.",
    )
    max_duration = serializers.IntegerField(
        min_value=0,
        required=False,
        help_text="Latest call at most this many seconds long, inclusive.",
    )

    def validate(self, attrs):
        low, high = attrs.get("min_duration"), attrs.get("max_duration")
        if low is not None and high is not None and low > high:
            raise serializers.ValidationError(
                {"max_duration": "max_duration must be greater than or equal to min_duration."}
            )
        return attrs


class PreviewAudienceSerializer(serializers.Serializer):
    """
    Input for the pre-create audience preview.

    Detail-false so the wizard can call it before a campaign exists.
    """

    contact_ids = serializers.ListField(
        child=serializers.UUIDField(), allow_empty=True
    )

    def validate_contact_ids(self, value):
        cap = getattr(settings, "CAMPAIGN_MAX_SELECTED_CONTACTS", 5_000)
        if len(value) > cap:
            raise serializers.ValidationError(
                f"A campaign cannot target more than {cap} contacts at once."
            )
        return value


class CampaignNameCheckSerializer(serializers.Serializer):
    """The body of ``POST /campaigns/check-name/``."""

    name = serializers.CharField(max_length=100)
    exclude_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text=(
            "The campaign being renamed, so its own current name does not count as "
            "taken. Omit when checking a name for a new campaign."
        ),
    )
