from django.db import transaction
from rest_framework import serializers

from core.models.business_hours import BusinessHours, BusinessHoursInterval
from onboard.models.timezone import Timezone
from onboard.serializers.timezone import TimezoneRefSerializer
from tenant.common import TenantBaseModelSerializer
from utils.serializers.hybrid_related_field import HybridRelatedField

DAY_SECONDS = 24 * 3600
WEEK_SECONDS = 7 * DAY_SECONDS


def _seconds(value):
    return value.hour * 3600 + value.minute * 60 + value.second


def _placed(weekday, opens_at, closes_at):
    """``(offset, end)`` on a 0..WEEK_SECONDS timeline.

    A block that wraps midnight simply extends past its weekday's end;
    ``00:00 -> 00:00`` becomes a full 24 hours, which is how seven such blocks
    give true 24/7.
    """
    start = weekday * DAY_SECONDS + _seconds(opens_at)
    length = _seconds(closes_at) - _seconds(opens_at)
    if length <= 0:
        length += DAY_SECONDS
    return start, start + length


class BusinessHoursIntervalSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessHoursInterval
        fields = ("opens_at", "closes_at")

    # No per-interval closes_at > opens_at check: that is precisely the rule that
    # would forbid an overnight block. `closes_at <= opens_at` means "closes the
    # next day", and coherence is enforced across the whole week in
    # BusinessHoursSerializer.validate_schedule.


class DayScheduleSerializer(serializers.Serializer):
    """One weekday and its open blocks. An empty `intervals` list means closed."""

    weekday = serializers.IntegerField(min_value=0, max_value=6)
    intervals = BusinessHoursIntervalSerializer(many=True)


class BusinessHoursSerializer(TenantBaseModelSerializer):
    """
    The tenant's opening hours, read and written as one document.

    `schedule` is **always seven entries on the way out**, in weekday order, even
    though closed days store no rows. The grid the tenant edits has seven rows
    whatever the data says, and making the client synthesise the missing ones is
    how Sunday ends up absent from the UI.

    On the way in it is a **full replacement**: whatever `schedule` contains
    becomes the week. A weekly grid is edited as a whole, and patching individual
    intervals would need stable ids for rows the client thinks of as cells.
    """

    timezone = HybridRelatedField(
        serializer=TimezoneRefSerializer,
        queryset=Timezone.objects.all(),
    )
    schedule = DayScheduleSerializer(many=True, required=False)

    class Meta(TenantBaseModelSerializer.Meta):
        model = BusinessHours
        fields = TenantBaseModelSerializer.Meta.fields + [
            "timezone",
            "is_open",
            "closed_message",
            "schedule",
        ]
        read_only_fields = TenantBaseModelSerializer.Meta.read_only_fields

    # --- output ---------------------------------------------------------------

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["schedule"] = self._expanded_schedule(instance)
        return data

    @staticmethod
    def _expanded_schedule(instance):
        by_weekday = {}
        for interval in instance.intervals.all():
            by_weekday.setdefault(interval.weekday, []).append(interval)

        schedule = []
        for weekday, label in BusinessHoursInterval.Weekday.choices:
            intervals = sorted(by_weekday.get(weekday, []), key=lambda i: i.opens_at)
            schedule.append(
                {
                    "weekday": weekday,
                    "day": label,
                    # Derived, not stored: a closed day is the absence of rows, so
                    # this can never disagree with the intervals beside it.
                    "is_open": bool(intervals),
                    "intervals": BusinessHoursIntervalSerializer(
                        intervals, many=True
                    ).data,
                }
            )
        return schedule

    # --- validation -----------------------------------------------------------

    def validate_schedule(self, value):
        """Reject a duplicated weekday, and any overlap on a CIRCULAR week.

        Per-weekday sorting cannot see a Monday-overnight block colliding with
        Tuesday morning, nor Sunday night colliding with Monday. Placing every
        block on one 604 800-second timeline and comparing each pair against its
        neighbours a week either side catches all three cases identically.
        """
        weekdays = [day["weekday"] for day in value]
        duplicates = {day for day in weekdays if weekdays.count(day) > 1}
        if duplicates:
            raise serializers.ValidationError(
                f"Weekday {sorted(duplicates)[0]} appears more than once. Put all "
                "of a day's blocks in a single entry."
            )

        blocks = [
            (day["weekday"], interval["opens_at"], interval["closes_at"])
            for day in value
            for interval in day["intervals"]
        ]
        placed = [_placed(*block) for block in blocks]

        # Cheap over-specification guard before the O(n^2) pass (e.g. eight 24h
        # blocks): more than a week of coverage must contain an overlap.
        if sum(end - start for start, end in placed) > WEEK_SECONDS:
            raise serializers.ValidationError(
                "These hours cover more than a full week, so they must overlap."
            )

        for i, a in enumerate(placed):
            for j, b in enumerate(placed[i + 1:], start=i + 1):
                for shift in (-WEEK_SECONDS, 0, WEEK_SECONDS):
                    if a[0] < b[1] + shift and b[0] + shift < a[1]:
                        raise serializers.ValidationError(
                            "Overlapping opening hours: weekday "
                            f"{blocks[i][0]} conflicts with weekday {blocks[j][0]}."
                        )
        return value

    # --- persistence ----------------------------------------------------------

    def _replace_intervals(self, instance, schedule):
        """Swap the whole week atomically.

        Delete-then-recreate rather than a diff: the client sends the grid it
        wants, and matching incoming blocks to existing rows would need ids for
        things the tenant experiences as cells in a table.

        Hard delete through ``all_objects``. ``intervals`` resolves to the
        soft-delete-aware manager, whose ``delete()`` raises by design — and a
        soft delete would be wrong here anyway: these rows are replaceable
        configuration, not history, so tombstoning every edit of the weekly grid
        would accumulate invisible rows forever.
        """
        BusinessHoursInterval.all_objects.filter(business_hours=instance).delete()
        BusinessHoursInterval.objects.bulk_create(
            [
                BusinessHoursInterval(
                    tenant=instance.tenant,
                    business_hours=instance,
                    weekday=day["weekday"],
                    opens_at=interval["opens_at"],
                    closes_at=interval["closes_at"],
                )
                for day in schedule
                for interval in day["intervals"]
            ]
        )

    @transaction.atomic
    def create(self, validated_data):
        schedule = validated_data.pop("schedule", [])
        instance = super().create(validated_data)
        self._replace_intervals(instance, schedule)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        schedule = validated_data.pop("schedule", None)
        instance = super().update(instance, validated_data)
        # None means "not mentioned" and leaves the week alone; an empty list is a
        # deliberate "closed all week" and must clear it.
        if schedule is not None:
            self._replace_intervals(instance, schedule)
        return instance
