from django.urls import path, include

app_name = "core"

# Consolidated core app patterns
urlpatterns = [
    path("", include("core.urls.knowledge_base")),
]
