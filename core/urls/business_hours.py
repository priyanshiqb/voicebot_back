from django.urls import path

from core.views.business_hours import BusinessHoursView

urlpatterns = [
    path("business-hours/", BusinessHoursView.as_view(), name="business-hours"),
]
