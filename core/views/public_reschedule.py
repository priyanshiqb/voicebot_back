import logging
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from core.services import rebooking
from core.services.exceptions import (
    ExternalCalendarUnavailable,
    NoEligibleHostError,
    PolicyViolationError,
    SlotConflictError,
)

logger = logging.getLogger(__name__)


from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers

class RescheduleRateThrottle(AnonRateThrottle):
    scope = "public_reschedule"

    def get_rate(self):
        return "30/min"


class PublicRescheduleInputSerializer(serializers.Serializer):
    """
    Validates the chosen slot.

    A DRF ``DateTimeField`` is deliberate rather than hand-parsing: with
    ``USE_TZ=True`` it returns an AWARE datetime, coercing a value that carries no
    offset instead of passing a naive one through. Hand-parsing here previously
    accepted naive input, which then hit ``start_time < now`` in the booking
    service and raised ``TypeError`` — an uncaught 500 on the only unauthenticated
    endpoint in the module, triggered by nothing worse than a client omitting the
    ``Z`` suffix.
    """

    start_time = serializers.DateTimeField()


@extend_schema(tags=["Booking"])
class PublicRescheduleView(APIView):
    """
    Public, unauthenticated rebooking behind a single-use reschedule token.
      GET  -> booking summary + available slots
      POST -> book the chosen slot, retire the old booking, consume the token
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [RescheduleRateThrottle]

    @extend_schema(
        responses={
            200: inline_serializer(
                name="PublicRescheduleGetResponse",
                fields={
                    "status": serializers.BooleanField(),
                    "data": serializers.DictField()
                }
            )
        }
    )
    def get(self, request, token):
        now = timezone.now()
        try:
            booking = rebooking.get_context(str(token), now=now)
        except rebooking.InvalidRescheduleToken as exc:
            return Response({"status": False, "message": str(exc)}, status=400)

        template = booking.event_template
        # UTC instants, matching the authenticated slots endpoint: the customer's
        # browser knows their timezone and resolves their chosen day to instants.
        # Defaults span from now to the template's horizon, so a client that sends
        # nothing still gets a usable page.
        horizon = now + timedelta(days=template.max_horizon_days)
        range_start = parse_datetime(request.query_params.get("start_utc", "") or "") or now
        range_end = (
            parse_datetime(request.query_params.get("end_utc", "") or "") or horizon
        )
        if timezone.is_naive(range_start):
            range_start = range_start.replace(tzinfo=dt_timezone.utc)
        if timezone.is_naive(range_end):
            range_end = range_end.replace(tzinfo=dt_timezone.utc)
        range_start = max(range_start, now)
        range_end = min(range_end, horizon)
        if range_end <= range_start:
            range_end = range_start + timedelta(days=1)

        _, slots = rebooking.list_slots(str(token), range_start, range_end, now=now)
        return Response(
            {
                "status": True,
                "data": {
                    "booking": {
                        "id": str(booking.id),
                        "title": booking.title,
                        "original_start_time": booking.start_time,
                        "event_type": template.name,
                        "duration_minutes": template.duration_minutes,
                    },
                    "slots": [
                        {"start": s.start, "end": s.end} for s in slots
                    ],
                },
            },
            status=200,
        )

    @extend_schema(
        request=inline_serializer(
            name="PublicReschedulePostRequest",
            fields={"start_time": serializers.DateTimeField()}
        ),
        responses={
            201: inline_serializer(
                name="PublicReschedulePostResponse",
                fields={
                    "status": serializers.BooleanField(),
                    "message": serializers.CharField(),
                    "data": serializers.DictField()
                }
            )
        }
    )
    def post(self, request, token):
        payload = PublicRescheduleInputSerializer(data=request.data)
        if not payload.is_valid():
            return Response(
                {
                    "status": False,
                    "message": "A valid start_time is required.",
                    "errors": payload.errors,
                },
                status=400,
            )
        start_time = payload.validated_data["start_time"]
        try:
            new = rebooking.rebook(str(token), start_time)
        except rebooking.InvalidRescheduleToken as exc:
            return Response({"status": False, "message": str(exc)}, status=400)
        except (PolicyViolationError, NoEligibleHostError) as exc:
            return Response({"status": False, "message": str(exc)}, status=400)
        except SlotConflictError as exc:
            return Response({"status": False, "message": str(exc)}, status=409)
        except ExternalCalendarUnavailable as exc:
            # 503: the slot may be free; the host's calendar just could not be read.
            return Response({"status": False, "message": str(exc)}, status=503)

        booking_id = new[0].id if isinstance(new, list) else new.id
        return Response(
            {
                "status": True,
                "message": "Your booking has been rescheduled.",
                "data": {"booking_id": str(booking_id)},
            },
            status=201,
        )
