from django.urls import path, include

app_name = "core"

# Consolidated core app patterns
urlpatterns = [
    path("", include("core.urls.knowledge_base")),
    path("", include("core.urls.agent_template")),
    path("", include("core.urls.probing_question")),
    path("", include("core.urls.agent")),
    path("", include("core.urls.agent_call_log")),
]
