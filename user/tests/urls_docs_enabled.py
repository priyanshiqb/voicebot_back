"""The URLconf a deployment with `API_DOCS_ENABLED=True` registers.

Routes are built when the URLconf is imported, so flipping the setting inside a test
cannot re-register them. This module is that branch of `config.urls`, for the one test
that checks the public paths are anonymous when a deployment asks for them.
"""

from rest_framework.permissions import AllowAny

from config.urls import docs_patterns

urlpatterns = docs_patterns(
    "api/", permission_classes=[AllowAny], authentication_classes=[]
)
