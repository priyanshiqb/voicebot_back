from django.urls import path, include

app_name = "core"

# Consolidated core app patterns
urlpatterns = [
    path("", include("core.urls.knowledge_base")),
    path("", include("core.urls.agent_template")),
    path("", include("core.urls.probing_question")),
    path("", include("core.urls.agent")),
    path("", include("core.urls.agent_call_log")),
    path("", include("core.urls.custom_voice")),
    # Scheduling & Booking (Phase 1)
    path("", include("core.urls.event_template")),
    path("", include("core.urls.user_event_skill")),
    path("", include("core.urls.availability_profile")),
    path("", include("core.urls.availability_exception")),
    path("", include("core.urls.availability")),
    path("", include("core.urls.booking")),
    path("", include("core.urls.calendar_integration")),
    path("", include("core.urls.calendar_webhook")),
    path("", include("core.urls.device_token")),
    path("", include("core.urls.in_app_notification")),
    path("", include("core.urls.public_reschedule")),
    # Contacts
    path("", include("core.urls.contact")),
    path("", include("core.urls.directory_contact")),
    path("", include("core.urls.business_hours")),
    path("", include("core.urls.contact_import")),
    # Campaigns
    path("", include("core.urls.dashboard")),
    path("", include("core.urls.campaign")),
    path("", include("core.urls.campaign_runner")),
    # Product & Service Catalog
    path("", include("core.urls.catalog")),
]
