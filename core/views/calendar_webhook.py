import logging

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models.calendar_integration import CalendarIntegration

logger = logging.getLogger(__name__)


class GoogleCalendarWebhookView(APIView):
    """
    Public endpoint Google POSTs calendar-change notifications to. Unauthenticated
    (Google can't carry our auth header) but validated against the per-channel
    X-Goog-Channel-Token we set when creating the watch. Always returns 200 so
    Google stops retrying; unknown/invalid channels are simply ignored.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        channel_id = request.headers.get("X-Goog-Channel-ID", "")
        token = request.headers.get("X-Goog-Channel-Token", "")
        state = request.headers.get("X-Goog-Resource-State", "")

        if not channel_id:
            return Response(status=200)

        integration = CalendarIntegration.objects.filter(
            watch_channel_id=channel_id, is_deleted=False
        ).first()
        if integration is None or (
            integration.watch_token and integration.watch_token != token
        ):
            logger.warning(
                "Ignoring calendar webhook for unknown/invalid channel %s", channel_id
            )
            return Response(status=200)

        # "sync" is Google's initial handshake ping — nothing changed yet.
        if state and state != "sync":
            from core.tasks import incremental_calendar_sync

            incremental_calendar_sync.delay(str(integration.id))

        return Response(status=200)
