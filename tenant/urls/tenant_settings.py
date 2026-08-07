from django.urls import path

from tenant.views.tenant_settings import TenantSettingsView

urlpatterns = [
    path("settings/", TenantSettingsView.as_view(), name="tenant-settings"),
]
