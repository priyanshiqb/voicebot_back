"""The single ``/api/v1/ops/`` router.

Everything platform staff can reach is mounted here and nowhere else. That is the
point of the app: "show me every operator endpoint" is this file plus what it includes,
rather than a grep across five apps.
"""

from django.urls import include, path

app_name = "ops"

urlpatterns = [
    path("", include("ops.urls.plan")),
    path("", include("ops.urls.tenant")),
]
