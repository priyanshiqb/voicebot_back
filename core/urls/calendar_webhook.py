from django.urls import path

from core.views.calendar_webhook import GoogleCalendarWebhookView

urlpatterns = [
    path(
        "calendar-webhook/google/",
        GoogleCalendarWebhookView.as_view(),
        name="calendar-webhook-google",
    ),
]
