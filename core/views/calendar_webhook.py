import logging

from django.http import HttpResponse
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


class MicrosoftCalendarWebhookView(APIView):
    """
    Public endpoint Microsoft Graph POSTs calendar-change notifications to.
    Unauthenticated (Graph can't carry our auth header) but validated against the
    per-subscription `clientState` we set when creating the watch.

    Two request shapes arrive here:
      1. Validation handshake — on subscription creation Graph calls this URL with
         `?validationToken=...` and expects it echoed back as text/plain (200)
         within 10s, or the subscription is rejected.
      2. Change notification — JSON body `{"value": [{subscriptionId, clientState,
         resourceData: {id}}]}`. We match the subscription, verify `clientState`,
         and enqueue an incremental sync. Unknown/invalid subscriptions are ignored.
    Always responds 2xx so Graph stops retrying.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        # (1) Validation handshake — must be checked before touching request.data
        # (the validation request is text/plain, which has no DRF parser).
        validation_token = request.query_params.get("validationToken")
        if validation_token is not None:
            return HttpResponse(validation_token, content_type="text/plain", status=200)

        # (2) Change notifications.
        for note in request.data.get("value") or []:
            subscription_id = note.get("subscriptionId")
            if not subscription_id:
                continue
            integration = CalendarIntegration.objects.filter(
                watch_channel_id=subscription_id, is_deleted=False
            ).first()
            if integration is None or (
                integration.watch_token
                and integration.watch_token != note.get("clientState")
            ):
                logger.warning(
                    "Ignoring MS calendar webhook for unknown/invalid subscription %s",
                    subscription_id,
                )
                continue
            from core.tasks import incremental_calendar_sync

            incremental_calendar_sync.delay(str(integration.id))

        return Response(status=202)
