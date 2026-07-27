import logging
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from core.services import rebooking
from core.services.exceptions import (
    NoEligibleHostError,
    PolicyViolationError,
    SlotConflictError,
)

logger = logging.getLogger(__name__)


class RescheduleRateThrottle(AnonRateThrottle):
    scope = "public_reschedule"

    def get_rate(self):
        return "30/min"


class PublicRescheduleView(APIView):
    """
    Public, unauthenticated rebooking behind a single-use reschedule token.
      GET  -> booking summary + available slots
      POST -> book the chosen slot, retire the old booking, consume the token
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [RescheduleRateThrottle]

    def get(self, request, token):
        now = timezone.now()
        try:
            booking = rebooking.get_context(str(token), now=now)
        except rebooking.InvalidRescheduleToken as exc:
            return Response({"status": False, "message": str(exc)}, status=400)

        template = booking.event_template
        date_from = parse_date(request.query_params.get("date_from", "")) or now.date()
        default_to = now.date() + timedelta(days=template.max_horizon_days)
        date_to = parse_date(request.query_params.get("date_to", "")) or default_to
        if date_to < date_from:
            date_to = date_from
        # never look past the horizon
        horizon = now.date() + timedelta(days=template.max_horizon_days)
        if date_to > horizon:
            date_to = horizon

        _, slots = rebooking.list_slots(str(token), date_from, date_to, now=now)
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

    def post(self, request, token):
        raw_start = request.data.get("start_time")
        start_time = parse_datetime(raw_start) if raw_start else None
        if start_time is None:
            return Response(
                {"status": False, "message": "A valid start_time is required."},
                status=400,
            )
        try:
            new = rebooking.rebook(str(token), start_time)
        except rebooking.InvalidRescheduleToken as exc:
            return Response({"status": False, "message": str(exc)}, status=400)
        except (PolicyViolationError, NoEligibleHostError) as exc:
            return Response({"status": False, "message": str(exc)}, status=400)
        except SlotConflictError as exc:
            return Response({"status": False, "message": str(exc)}, status=409)

        booking_id = new[0].id if isinstance(new, list) else new.id
        return Response(
            {
                "status": True,
                "message": "Your booking has been rescheduled.",
                "data": {"booking_id": str(booking_id)},
            },
            status=201,
        )
