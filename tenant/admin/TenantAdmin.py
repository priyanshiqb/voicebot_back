from django.contrib import admin, messages
from utils.admin.base_admin_core import BaseAdminCore
from utils.throttling import clear_smtp_test_throttles
from tenant.models.Tenant import Tenant
from tenant.models.TenantUserMapping import TenantUserMapping


@admin.register(Tenant)
class TenantAdmin(BaseAdminCore):
    """
    Administrative display dashboard for Tenant resources.
    Uses BaseAdminCore for soft-delete hooks, search fields, and unified logs views.
    """
    list_display = ("name", "subdomain", "is_active", "is_deleted", "created_at")
    list_filter = ("is_active", "is_deleted", "created_at")
    search_fields = ("name", "subdomain", "id")

    actions = BaseAdminCore.actions + ("clear_smtp_test_rate_limit",)

    @admin.action(
        description="Clear SMTP test rate limit for all users of selected tenants"
    )
    def clear_smtp_test_rate_limit(self, request, queryset):
        """The tenant-wide version of the per-user action on the user admin.

        The limit is keyed per user, not per tenant, so "this tenant is throttled"
        really means "some of its users are" — and whoever reports it rarely knows
        which. This resolves the tenant's members and clears them all.

        Members come from active mappings only. A soft-deleted mapping is someone
        who is no longer part of the tenant, and their counter is not this tenant's
        to reset.
        """
        user_ids = list(
            TenantUserMapping.objects.filter(tenant__in=queryset).values_list(
                "user_id", flat=True
            )
        )

        if not user_ids:
            self.message_user(
                request,
                f"No active users found for {queryset.count()} tenant(s).",
                messages.WARNING,
            )
            return

        cleared = clear_smtp_test_throttles(user_ids)

        if cleared:
            self.message_user(
                request,
                f"Cleared the SMTP test rate limit for {cleared} of {len(user_ids)} "
                f"user(s) across {queryset.count()} tenant(s).",
                messages.SUCCESS,
            )
        else:
            self.message_user(
                request,
                f"None of the {len(user_ids)} user(s) in {queryset.count()} "
                f"tenant(s) were rate limited on the SMTP test endpoint.",
                messages.INFO,
            )

    def get_actions(self, request):
        """Superusers only — see `CustomUserAdmin.get_actions`.

        It matters more here: a tenant admin able to clear their own tenant's
        limit would be clearing their own, which is the definition of not being
        throttled.
        """
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop("clear_smtp_test_rate_limit", None)
        return actions
