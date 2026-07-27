from django.urls import path

from core.views.public_reschedule import PublicRescheduleView

urlpatterns = [
    path(
        "public/reschedule/<uuid:token>/",
        PublicRescheduleView.as_view(),
        name="public-reschedule",
    ),
]
