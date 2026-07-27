from django.urls import path

from core.views.calendar_webhook import (
    GoogleCalendarWebhookView,
    MicrosoftCalendarWebhookView,
)

urlpatterns = [
    path(
        "calendar-webhook/google/",
        GoogleCalendarWebhookView.as_view(),
        name="calendar-webhook-google",
    ),
    path(
        "calendar-webhook/microsoft/",
        MicrosoftCalendarWebhookView.as_view(),
        name="calendar-webhook-microsoft",
    ),
]
